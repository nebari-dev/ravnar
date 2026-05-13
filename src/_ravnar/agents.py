from __future__ import annotations

import abc
import textwrap
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import ag_ui.core
import pydantic
from opentelemetry import trace

from .mixin import SetupTeardownMixin

tracer = trace.get_tracer(__name__)

if TYPE_CHECKING:
    import agno.agent
    import pydantic_ai

    from _ravnar.schema import QuickPrompt


class Agent(abc.ABC, SetupTeardownMixin):
    """Agent base class"""

    @abc.abstractmethod
    def run(self, input: ag_ui.core.RunAgentInput) -> AsyncIterator[ag_ui.core.Event]: ...

    def get_capabilities(self) -> ag_ui.core.AgentCapabilities:
        """The capabilities of the agent."""
        return ag_ui.core.AgentCapabilities(transport=ag_ui.core.TransportCapabilities(streaming=True))

    def get_quick_prompts(self) -> list[QuickPrompt]:
        """The quick prompts of the agent."""
        return []


class DefaultAgent(Agent):
    async def run(self, input: ag_ui.core.RunAgentInput) -> AsyncIterator[ag_ui.core.Event]:
        span = tracer.start_span("DefaultAgent.run")
        span.set_attribute("agent_class", "DefaultAgent")
        try:
            message_id = str(uuid.uuid4())
            message = """
            Hello, I'm ravnar's default agent.
            Unfortunately, I'm not terribly helpful right now.
            """

            yield ag_ui.core.RunStartedEvent(
                thread_id=input.thread_id, run_id=input.run_id, parent_run_id=input.parent_run_id
            )
            yield ag_ui.core.TextMessageStartEvent(message_id=message_id)
            for delta in textwrap.dedent(message.strip()).split():
                yield ag_ui.core.TextMessageContentEvent(message_id=message_id, delta=delta)
            yield ag_ui.core.TextMessageEndEvent(message_id=message_id)
            yield ag_ui.core.RunFinishedEvent(thread_id=input.thread_id, run_id=input.run_id)
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(trace.StatusCode.ERROR, description=str(exc))
            raise
        finally:
            span.end()


class _AgentBase(Agent):
    def __init__(
        self,
        *,
        capabilities: ag_ui.core.AgentCapabilities | None = None,
        quick_prompts: list[QuickPrompt] | None = None,
    ):
        if capabilities is None:
            capabilities = super().get_capabilities()
        self._capabilities = capabilities

        if quick_prompts is None:
            quick_prompts = super().get_quick_prompts()
        self._quick_prompts = quick_prompts

    def get_capabilities(self) -> ag_ui.core.AgentCapabilities:
        """The capabilities of the agent."""
        return self._capabilities

    def get_quick_prompts(self) -> list[QuickPrompt]:
        """The quick prompts of the agent."""
        return self._quick_prompts


class SSEAgent(_AgentBase):
    """SSE Agent"""

    def __init__(
        self,
        method: str,
        url: str,
        *,
        client_kwargs: dict[str, Any] | None = None,
        capabilities: ag_ui.core.AgentCapabilities | None = None,
        quick_prompts: list[QuickPrompt] | None = None,
    ):
        self._method = method
        self._url = url
        if client_kwargs is None:
            client_kwargs = {}
        self._client_kwargs = client_kwargs

        super().__init__(capabilities=capabilities, quick_prompts=quick_prompts)

    async def run(self, input: ag_ui.core.RunAgentInput) -> AsyncIterator[ag_ui.core.Event]:
        span = tracer.start_span("SSEAgent.run")
        span.set_attribute("agent_class", "SSEAgent")
        try:
            import httpx
            import httpx_sse

            async with (
                httpx.AsyncClient() as client,
                httpx_sse.aconnect_sse(
                    client,
                    self._method,
                    self._url,
                    json=input.model_dump(mode="json"),
                ) as event_source,
            ):
                event_source.response.raise_for_status()

                ta: pydantic.TypeAdapter[ag_ui.core.Event] = pydantic.TypeAdapter(ag_ui.core.Event)
                async for sse in event_source.aiter_sse():
                    yield ta.validate_json(sse.data)
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(trace.StatusCode.ERROR, description=str(exc))
            raise
        finally:
            span.end()


class PydanticAiAgentWrapper(_AgentBase):
    """Pydantic AI agent wrapper"""

    def __init__(
        self,
        agent: pydantic_ai.Agent,
        *,
        capabilities: ag_ui.core.AgentCapabilities | None = None,
        quick_prompts: list[QuickPrompt] | None = None,
    ) -> None:
        self._agent = agent

        if capabilities is None:
            capabilities = ag_ui.core.AgentCapabilities(
                identity=ag_ui.core.IdentityCapabilities(name=agent.name),
                transport=ag_ui.core.TransportCapabilities(streaming=True),
            )

        super().__init__(capabilities=capabilities, quick_prompts=quick_prompts)

    def run(self, input: ag_ui.core.RunAgentInput) -> AsyncIterator[ag_ui.core.Event]:
        span = tracer.start_span("PydanticAiAgentWrapper.run")
        span.set_attribute("agent_class", "PydanticAiAgentWrapper")
        if self._agent.name is not None:
            span.set_attribute("agent_name", self._agent.name)

        async def _gen() -> AsyncIterator[ag_ui.core.Event]:
            try:
                from pydantic_ai.ui.ag_ui import AGUIAdapter

                async for event in AGUIAdapter(
                    agent=self._agent, run_input=input, accept="text/event-stream"
                ).run_stream():
                    yield event  # type: ignore[misc]
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, description=str(exc))
                raise
            finally:
                span.end()

        return _gen()


class AgnoAgentWrapper(_AgentBase):
    """Agno agent wrapper"""

    def __init__(
        self,
        agent: agno.agent.Agent,
        *,
        capabilities: ag_ui.core.AgentCapabilities | None = None,
        quick_prompts: list[QuickPrompt] | None = None,
    ) -> None:
        self._agent = agent

        if capabilities is None:
            capabilities = ag_ui.core.AgentCapabilities(
                identity=ag_ui.core.IdentityCapabilities(name=agent.name),
                transport=ag_ui.core.TransportCapabilities(streaming=True),
            )

        super().__init__(capabilities=capabilities, quick_prompts=quick_prompts)

    def run(self, input: ag_ui.core.RunAgentInput) -> AsyncIterator[ag_ui.core.Event]:
        span = tracer.start_span("AgnoAgentWrapper.run")
        span.set_attribute("agent_class", "AgnoAgentWrapper")
        if self._agent.name is not None:
            span.set_attribute("agent_name", self._agent.name)

        async def _gen() -> AsyncIterator[ag_ui.core.Event]:
            try:
                from agno.os.interfaces.agui.router import run_agent

                async for event in run_agent(self._agent, input):
                    yield event  # type: ignore[misc]
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, description=str(exc))
                raise
            finally:
                span.end()

        return _gen()
