from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, cast

import ag_ui.core
import ag_ui.encoder
import fastsse
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from _ravnar import schema
from _ravnar.auth import make_authorized_user_factory
from _ravnar.events import EventProcessor
from _ravnar.mixin import SetupTeardownMixin
from _ravnar.observability import configure_logging, configure_tracing
from _ravnar.utils import as_awaitable

from .api import make_router as make_api_router
from .config import AgentConfig, BaseConfig, Config
from .version import __version__

if TYPE_CHECKING:
    from _ravnar.agents import Agent

tracer = trace.get_tracer(__name__)


class Ravnar:
    def __init__(self, config: BaseConfig | None = None) -> None:
        if config is None:
            config = Config.parse()

        configure_logging(config)
        configure_tracing(config)

        self.config = config
        self.app = self._make_app(config)

    def _make_app(self, config: BaseConfig) -> FastAPI:
        agent_handler = AgentHandler(config.agents)

        app = FastAPI(
            title="ravnar",
            version=__version__,
            lifespan=SetupTeardownMixin.lifespan_factory(agent_handler),
            root_path=config.server.root_path,
        )

        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.security.cors.allowed_origins,
            allow_headers=[*config.security.cors.allowed_headers],
            allow_methods=["*"],
        )

        authorized_user_with = make_authorized_user_factory(config.security)

        @app.get("/", include_in_schema=False)
        async def base_redirect() -> RedirectResponse:
            return RedirectResponse(f"{app.root_path}/docs", status_code=status.HTTP_302_FOUND)

        @app.get("/health")
        async def health() -> Response:
            return Response(b"", status_code=status.HTTP_200_OK)

        @app.get("/version")
        async def version() -> str:
            return __version__

        app.include_router(
            make_api_router(
                storage_config=config.storage,
                agent_handler=agent_handler,
                authorized_user_with=authorized_user_with,
            ),
            prefix="/api",
        )

        # We want to include some prefixes, but the instrumentor only lets us exclude URLs. We achieve what we want by
        # building a negative regex that matches all URLs except for the prefixes we want to include
        included_prefixes = ["/auth", "/api"]
        excluded_urls = rf"^https?://[^/]+(?:/?$|/(?!({'|'.join(p.lstrip('/') for p in included_prefixes)})/).*$)"
        FastAPIInstrumentor.instrument_app(app, excluded_urls=excluded_urls)

        return app

    def serve(self) -> None:
        import uvicorn

        uvicorn.run(
            self.app,
            host=self.config.server.hostname,
            port=self.config.server.port,
            proxy_headers=self.config.server.proxy_headers,
            forwarded_allow_ips=self.config.server.forwarded_allow_ips,
            log_config=None,
            use_colors=False,
        )


class AgentHandler(SetupTeardownMixin):
    def __init__(self, agent_config: AgentConfig) -> None:
        self._static_agents: dict[str, Agent] = {id: factory() for id, factory in agent_config.static.items()}
        self._dynamic_agents: dict[str, Agent] = {}
        self._event_encoder = ag_ui.encoder.EventEncoder()
        self._dynamic_enabled = agent_config.dynamic.enabled

    @staticmethod
    async def _setup_agent(agent: Agent) -> None:
        await as_awaitable(agent.setup)

    @staticmethod
    async def _teardown_agent(agent: Agent) -> None:
        await as_awaitable(agent.teardown)

    async def setup(self) -> None:  # type: ignore[override]
        await asyncio.gather(*[self._setup_agent(agent) for agent in self._static_agents.values()])

    async def teardown(self) -> None:  # type: ignore[override]
        await asyncio.gather(
            *[self._teardown_agent(agent) for agent in (*self._static_agents.values(), *self._dynamic_agents.values())]
        )

    def infos(self) -> list[schema.AgentInfo]:
        agents = dict(self._static_agents)
        if self._dynamic_enabled:
            agents.update(self._dynamic_agents)
        return [
            schema.AgentInfo(
                id=id,
                capabilities=agent.get_capabilities(),
                quick_prompts=agent.get_quick_prompts(),
            )
            for id, agent in agents.items()
        ]

    @property
    def dynamic_enabled(self) -> bool:
        return self._dynamic_enabled

    def _get_agent(self, agent_id: str) -> Agent:
        if agent_id in self._static_agents:
            return self._static_agents[agent_id]
        if agent_id in self._dynamic_agents:
            return self._dynamic_agents[agent_id]
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    def assert_available(self, agent_id: str) -> None:
        self._get_agent(agent_id)

    async def add_agent(self, agent_id: str, agent: Agent) -> None:
        if agent_id in self._static_agents or agent_id in self._dynamic_agents:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent ID already exists")
        self._dynamic_agents[agent_id] = agent
        await self._setup_agent(agent)

    async def remove_agent(self, agent_id: str) -> None:
        if agent_id in self._static_agents:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Static agents cannot be deleted")
        if agent_id not in self._dynamic_agents:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
        agent = self._dynamic_agents.pop(agent_id)
        await self._teardown_agent(agent)

    def _sse_encoder(self, data: fastsse.Data) -> bytes:
        return self._event_encoder.encode(cast(ag_ui.core.Event, data)).encode()

    async def run(
        self,
        agent_id: str,
        run_agent_input: ag_ui.core.RunAgentInput,
        *,
        callback: Callable[[EventProcessor], Awaitable[None]] | None = None,
    ) -> fastsse.Response:
        agent = self._get_agent(agent_id)

        event_processor = EventProcessor(run_agent_input=run_agent_input)

        span = tracer.start_span("AgentHandler.run")
        span.set_attribute("agent_id", agent_id)
        span.set_attribute("thread_id", run_agent_input.thread_id)
        span.set_attribute("run_id", run_agent_input.run_id)
        if run_agent_input.parent_run_id is not None:
            span.set_attribute("parent_run_id", run_agent_input.parent_run_id)

        async def event_stream() -> AsyncIterator[ag_ui.core.Event]:
            try:
                async for event in event_processor.process_event_stream(agent.run(run_agent_input)):
                    yield event

                if callback is not None:
                    await callback(event_processor)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, description=str(exc))
                raise
            finally:
                span.end()

        return fastsse.Response(event_stream(), encoder=self._sse_encoder)
