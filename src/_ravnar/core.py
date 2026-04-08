from __future__ import annotations

import functools
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, cast

import ag_ui.core
import ag_ui.encoder
import fastsse
from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from _ravnar import schema
from _ravnar.events import EventProcessor
from _ravnar.observability import configure_logging, configure_tracing
from _ravnar.utils import resolve_forward_references

from .api import make_router as make_api_router
from .config import BaseConfig, Config
from .database import Database
from .mixin import SetupTeardownMixin
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
        database = Database(url=str(self.config.storage.database_dsn))

        app = FastAPI(
            title="ravnar",
            version=__version__,
            lifespan=SetupTeardownMixin.lifespan_factory(database),
            root_path=config.server.root_path,
        )

        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.security.cors.allowed_origins,
            allow_headers=[*config.security.cors.allowed_headers],
            allow_methods=["*"],
        )

        authenticated_user: Callable[..., Awaitable[schema.User]]
        if config.security.authenticator is None:

            async def authenticated_user() -> schema.User:
                return schema.User.default()
        else:
            authenticator = config.security.authenticator()

            authenticated_user = tracer.start_as_current_span("authenticate")(
                resolve_forward_references(authenticator.authenticate)
            )

        @app.get("/", include_in_schema=False)
        async def base_redirect() -> RedirectResponse:
            return RedirectResponse(f"{app.root_path}/docs", status_code=status.HTTP_302_FOUND)

        @app.get("/health")
        async def health() -> Response:
            return Response(b"", status_code=status.HTTP_200_OK)

        @app.get("/version")
        async def version() -> str:
            return __version__

        agent_handler = AgentHandler(config.agents)

        app.include_router(
            make_api_router(
                database=database,
                agent_handler=agent_handler,
                authenticated_user=authenticated_user,
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


class AgentHandler:
    def __init__(self, agent_factories: Mapping[str, Callable[[], Agent]]) -> None:
        self._agents = {id: agent_factory() for id, agent_factory in agent_factories.items()}
        self._event_encoder = ag_ui.encoder.EventEncoder()

    @functools.cached_property
    def configs(self) -> list[schema.AgentConfig]:
        return [
            schema.AgentConfig(id=id, capabilities=agent.get_capabilities(), quick_prompts=agent.get_quick_prompts())
            for id, agent in self._agents.items()
        ]

    def assert_available(self, agent_id: str) -> None:
        if agent_id not in self._agents:
            raise Exception

    def _sse_encoder(self, data: fastsse.Data) -> bytes:
        return self._event_encoder.encode(cast(ag_ui.core.Event, data)).encode()

    async def run(
        self,
        agent_id: str,
        run_agent_input: ag_ui.core.RunAgentInput,
        *,
        callback: Callable[[EventProcessor], Awaitable[None]] | None = None,
    ) -> fastsse.Response:
        self.assert_available(agent_id)
        agent = self._agents[agent_id]

        event_processor = EventProcessor(
            thread_id=run_agent_input.thread_id,
            run_id=run_agent_input.run_id,
            parent_run_id=run_agent_input.parent_run_id,
            state=run_agent_input.state,
            messages=run_agent_input.messages,
        )

        async def event_stream() -> AsyncIterator[ag_ui.core.Event]:
            async for event in event_processor.process_event_stream(agent.run(run_agent_input)):
                yield event

            if callback is not None:
                await callback(event_processor)

        return fastsse.Response(event_stream(), encoder=self._sse_encoder)
