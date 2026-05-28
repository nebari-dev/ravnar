from __future__ import annotations

import abc
import dataclasses
import textwrap
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import ag_ui.core
import pydantic

from .mixin import SetupTeardownMixin

if TYPE_CHECKING:
    import agno.agent
    import agno.tools
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


@dataclasses.dataclass
class _PydanticAiDynamicCapabilities:
    tools: list[ag_ui.core.Tool]
    reasoning_supported: bool | None
    approvals: bool | None
    image_output: bool | None
    structured_output: bool | None


class PydanticAiAgentWrapper(Agent):
    """Pydantic AI agent wrapper"""

    def __init__(
        self,
        agent: pydantic_ai.Agent,
        *,
        capabilities: ag_ui.core.AgentCapabilities | None = None,
        quick_prompts: list[QuickPrompt] | None = None,
    ) -> None:
        self._agent = agent

        self._capabilities: ag_ui.core.AgentCapabilities
        if capabilities is not None:
            self._capabilities = capabilities

        if quick_prompts is None:
            quick_prompts = []
        self._quick_prompts = quick_prompts

    async def setup(self) -> None:
        if hasattr(self, "_capabilities"):
            return

        self._capabilities = await self.extract_capabilities(self._agent)

    def run(self, input: ag_ui.core.RunAgentInput) -> AsyncIterator[ag_ui.core.Event]:
        from pydantic_ai.ui.ag_ui import AGUIAdapter

        return AGUIAdapter(agent=self._agent, run_input=input, accept="text/event-stream").run_stream()  # type: ignore[return-value]

    def get_capabilities(self) -> ag_ui.core.AgentCapabilities:
        """The capabilities of the agent."""
        return self._capabilities

    def get_quick_prompts(self) -> list[QuickPrompt]:
        """The quick prompts of the agent."""
        return self._quick_prompts

    @staticmethod
    async def extract_capabilities(
        agent: pydantic_ai.Agent, *, ctx: pydantic_ai.RunContext | None = None
    ) -> ag_ui.core.AgentCapabilities:
        import pydantic_ai.models
        from pydantic_ai.usage import RunUsage

        capabilities = ag_ui.core.AgentCapabilities(
            identity=ag_ui.core.IdentityCapabilities(
                name=agent.name,
                description=agent.description,
                type="pydantic-ai",
            ),
            transport=ag_ui.core.TransportCapabilities(streaming=True),
            tools=ag_ui.core.ToolsCapabilities(
                supported=True,
                client_provided=True,
            ),
        )

        if ctx is None and isinstance(agent.model, pydantic_ai.models.Model):
            ctx = pydantic_ai.RunContext(deps=None, model=agent.model, usage=RunUsage())
        if ctx is not None:
            dynamic_capabilities = await PydanticAiAgentWrapper._extract_dynamic_capabilities(agent, ctx=ctx)

            assert capabilities.tools is not None
            capabilities.tools.items = dynamic_capabilities.tools
            if dynamic_capabilities.reasoning_supported is not None:
                capabilities.reasoning = ag_ui.core.ReasoningCapabilities(
                    supported=dynamic_capabilities.reasoning_supported
                )
            if dynamic_capabilities.approvals is not None:
                capabilities.human_in_the_loop = ag_ui.core.HumanInTheLoopCapabilities(
                    approvals=dynamic_capabilities.approvals
                )
            if dynamic_capabilities.image_output is not None:
                capabilities.multimodal = ag_ui.core.MultimodalCapabilities(
                    output=ag_ui.core.MultimodalOutputCapabilities(image=dynamic_capabilities.image_output)
                )
            if dynamic_capabilities.structured_output is not None:
                capabilities.output = ag_ui.core.OutputCapabilities(
                    structured_output=dynamic_capabilities.structured_output
                )

        return capabilities

    @staticmethod
    async def _extract_dynamic_capabilities(
        agent: pydantic_ai.Agent, *, ctx: pydantic_ai.RunContext
    ) -> _PydanticAiDynamicCapabilities:
        tools = [
            ag_ui.core.Tool(
                name=(td := tool.tool_def).name,
                description=td.description or "",
                parameters=td.parameters_json_schema,
            )
            for toolset in agent.toolsets
            for tool in (await toolset.get_tools(ctx)).values()
        ]

        reasoning_supported: bool | None = None
        approvals: bool | None = None
        image_output: bool | None = None

        if agent.root_capability is not None:
            from pydantic_ai.capabilities import HandleDeferredToolCalls, ImageGeneration, Thinking

            for capability in agent.root_capability.capabilities:
                if isinstance(capability, Thinking):
                    reasoning_supported = True
                elif isinstance(capability, HandleDeferredToolCalls):
                    approvals = True
                elif isinstance(capability, ImageGeneration):
                    image_output = True

        if not isinstance(agent.output_type, type):
            structured_output = None
        elif issubclass(agent.output_type, str):
            structured_output = False
        elif issubclass(agent.output_type, pydantic.BaseModel):
            structured_output = True
        else:
            structured_output = None

        return _PydanticAiDynamicCapabilities(
            tools=tools,
            reasoning_supported=reasoning_supported,
            approvals=approvals,
            image_output=image_output,
            structured_output=structured_output,
        )


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
            capabilities = self.extract_capabilities(agent)

        super().__init__(capabilities=capabilities, quick_prompts=quick_prompts)

    def run(self, input: ag_ui.core.RunAgentInput) -> AsyncIterator[ag_ui.core.Event]:
        from agno.os.interfaces.agui.router import run_agent

        return run_agent(self._agent, input)  # type: ignore[return-value]

    @staticmethod
    def extract_capabilities(agent: agno.agent.Agent) -> ag_ui.core.AgentCapabilities:
        from agno.tools import Function, Toolkit

        tools: list[ag_ui.core.Tool] | None = None
        approvals: bool | None = None
        if isinstance(agent.tools, list):
            tools = []
            approvals = False
            for tool in agent.tools:
                functions: list[Function]
                if isinstance(tool, Toolkit):
                    functions = [fn for fn in tool.functions.values() if isinstance(fn, Function)]
                elif isinstance(tool, Function):
                    functions = [tool]
                elif callable(tool):
                    functions = [Function.from_callable(tool)]
                else:
                    continue

                for fn in functions:
                    tools.append(
                        ag_ui.core.Tool(
                            name=fn.name,
                            description=fn.description or "",
                            parameters=fn.parameters,
                        )
                    )
                    if fn.requires_confirmation or fn.requires_user_input:
                        approvals = True

        return ag_ui.core.AgentCapabilities(
            identity=ag_ui.core.IdentityCapabilities(
                name=agent.name,
                description=agent.description,
                metadata=agent.metadata,
                type="agno",
            ),
            transport=ag_ui.core.TransportCapabilities(streaming=True),
            tools=ag_ui.core.ToolsCapabilities(
                supported=True,
                items=tools,
                client_provided=False,
            ),
            human_in_the_loop=ag_ui.core.HumanInTheLoopCapabilities(approvals=approvals)
            if approvals is not None
            else None,
            output=ag_ui.core.OutputCapabilities(structured_output=structured_output)
            if (structured_output := agent.structured_outputs is True or agent.output_schema is not None)
            else None,
            reasoning=ag_ui.core.ReasoningCapabilities(supported=agent.reasoning),
            multi_agent=ag_ui.core.MultiAgentCapabilities(
                supported=True,
                sub_agents=[
                    ag_ui.core.SubAgentInfo(
                        name=agent.reasoning_agent.name,
                        description=agent.reasoning_agent.description,
                    )
                ],
            )
            if agent.reasoning_agent is not None and agent.reasoning_agent.name is not None
            else None,
        )
