from __future__ import annotations

import abc
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
            capabilities = self.extract_capabilities(agent)

        super().__init__(capabilities=capabilities, quick_prompts=quick_prompts)

    def run(self, input: ag_ui.core.RunAgentInput) -> AsyncIterator[ag_ui.core.Event]:
        from pydantic_ai.ui.ag_ui import AGUIAdapter

        return AGUIAdapter(agent=self._agent, run_input=input, accept="text/event-stream").run_stream()  # type: ignore[return-value]

    @staticmethod
    def extract_capabilities(agent: pydantic_ai.Agent) -> ag_ui.core.AgentCapabilities:
        from pydantic_ai._output import TextOutputSchema

        # --- identity ---
        identity_kwargs: dict[str, Any] = {"name": agent.name}
        if agent.description is not None:
            identity_kwargs["description"] = agent.description
        if getattr(agent, "_metadata", None) is not None:
            identity_kwargs["metadata"] = agent._metadata
        identity_kwargs["type"] = "pydantic-ai"

        # --- tools ---
        tool_items: list[ag_ui.core.Tool] = []
        function_toolset = getattr(agent, "_function_toolset", None)
        if function_toolset is not None and hasattr(function_toolset, "tools"):
            for tool in function_toolset.tools.values():
                tool_items.append(
                    ag_ui.core.Tool(
                        name=tool.name,
                        description=tool.description or "",
                        parameters=tool.function_schema.json_schema,
                    )
                )

        tools_capabilities = ag_ui.core.ToolsCapabilities(
            supported=True,
            items=tool_items if tool_items else None,
            client_provided=True,
        )

        # --- output ---
        output_schema = getattr(agent, "_output_schema", None)
        structured_output: bool | None = None
        if output_schema is not None and not isinstance(output_schema, TextOutputSchema):
            structured_output = True

        output_capabilities: ag_ui.core.OutputCapabilities | None = None
        if structured_output is not None:
            output_capabilities = ag_ui.core.OutputCapabilities(structured_output=structured_output)

        # --- reasoning, human_in_the_loop, multimodal ---
        reasoning_supported: bool | None = None
        approvals: bool | None = None
        multimodal_image: bool | None = None

        root_cap = getattr(agent, "_root_capability", None)
        if root_cap is not None:
            capabilities_list = getattr(root_cap, "capabilities", [])
            for cap in capabilities_list:
                # Skip factory callables (they require RunContext to resolve)
                if callable(cap) and not hasattr(cap, "__class__"):
                    continue
                cap_type = type(cap)
                cap_module = cap_type.__module__
                cap_name = cap_type.__name__

                if "Thinking" in cap_name and "pydantic_ai" in cap_module:
                    reasoning_supported = True
                elif "HandleDeferredToolCalls" in cap_name and "pydantic_ai" in cap_module:
                    approvals = True
                elif "ImageGeneration" in cap_name and "pydantic_ai" in cap_module:
                    multimodal_image = True

        reasoning_capabilities: ag_ui.core.ReasoningCapabilities | None = None
        if reasoning_supported is not None:
            reasoning_capabilities = ag_ui.core.ReasoningCapabilities(supported=reasoning_supported)

        human_in_the_loop: ag_ui.core.HumanInTheLoopCapabilities | None = None
        if approvals is not None:
            human_in_the_loop = ag_ui.core.HumanInTheLoopCapabilities(approvals=approvals)

        multimodal_capabilities: ag_ui.core.MultimodalCapabilities | None = None
        if multimodal_image is not None:
            multimodal_capabilities = ag_ui.core.MultimodalCapabilities(
                output=ag_ui.core.MultimodalOutputCapabilities(image=multimodal_image)
            )

        return ag_ui.core.AgentCapabilities(
            identity=ag_ui.core.IdentityCapabilities(**identity_kwargs),
            transport=ag_ui.core.TransportCapabilities(streaming=True),
            tools=tools_capabilities,
            output=output_capabilities,
            reasoning=reasoning_capabilities,
            human_in_the_loop=human_in_the_loop,
            multimodal=multimodal_capabilities,
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

        # --- identity ---
        identity_kwargs: dict[str, Any] = {"name": agent.name}
        if agent.description is not None:
            identity_kwargs["description"] = agent.description
        if getattr(agent, "metadata", None) is not None:
            identity_kwargs["metadata"] = agent.metadata
        identity_kwargs["type"] = "agno"

        # --- tools ---
        tool_items: list[ag_ui.core.Tool] = []
        hitl_detected = False
        raw_tools = getattr(agent, "tools", None) or []

        for tool in raw_tools:
            if isinstance(tool, Toolkit):
                toolkit_functions = getattr(tool, "functions", {})
                for fn in toolkit_functions.values():
                    if isinstance(fn, Function):
                        tool_items.append(
                            ag_ui.core.Tool(
                                name=fn.name,
                                description=fn.description or "",
                                parameters=fn.parameters,
                            )
                        )
                        if (
                            getattr(fn, "requires_confirmation", False)
                            or getattr(fn, "requires_user_input", False)
                        ):
                            hitl_detected = True
            elif isinstance(tool, Function):
                tool_items.append(
                    ag_ui.core.Tool(
                        name=tool.name,
                        description=tool.description or "",
                        parameters=tool.parameters,
                    )
                )
                if (
                    getattr(tool, "requires_confirmation", False)
                    or getattr(tool, "requires_user_input", False)
                ):
                    hitl_detected = True
            elif callable(tool):
                fn = Function.from_callable(tool)
                tool_items.append(
                    ag_ui.core.Tool(
                        name=fn.name,
                        description=fn.description or "",
                        parameters=fn.parameters,
                    )
                )

        tools_capabilities = ag_ui.core.ToolsCapabilities(
            supported=True,
            items=tool_items if tool_items else None,
            client_provided=True,
        )

        # --- output ---
        structured_output: bool | None = None
        if getattr(agent, "structured_outputs", False) is True:
            structured_output = True
        elif getattr(agent, "output_schema", None) is not None:
            structured_output = True

        output_capabilities: ag_ui.core.OutputCapabilities | None = None
        if structured_output is not None:
            output_capabilities = ag_ui.core.OutputCapabilities(structured_output=structured_output)

        # --- reasoning ---
        reasoning_supported: bool | None = None
        reasoning_val = getattr(agent, "reasoning", None)
        if reasoning_val is not None:
            reasoning_supported = bool(reasoning_val)

        reasoning_capabilities: ag_ui.core.ReasoningCapabilities | None = None
        if reasoning_supported is not None:
            reasoning_capabilities = ag_ui.core.ReasoningCapabilities(supported=reasoning_supported)

        # --- multiAgent ---
        multi_agent_capabilities: ag_ui.core.MultiAgentCapabilities | None = None
        reasoning_agent = getattr(agent, "reasoning_agent", None)
        if reasoning_agent is not None:
            sub_agent_info = ag_ui.core.SubAgentInfo(
                name=reasoning_agent.name,
                description=getattr(reasoning_agent, "description", None),
            )
            multi_agent_capabilities = ag_ui.core.MultiAgentCapabilities(
                supported=True,
                sub_agents=[sub_agent_info],
            )

        # --- human_in_the_loop ---
        human_in_the_loop: ag_ui.core.HumanInTheLoopCapabilities | None = None
        if hitl_detected:
            human_in_the_loop = ag_ui.core.HumanInTheLoopCapabilities(approvals=True)

        return ag_ui.core.AgentCapabilities(
            identity=ag_ui.core.IdentityCapabilities(**identity_kwargs),
            transport=ag_ui.core.TransportCapabilities(streaming=True),
            tools=tools_capabilities,
            output=output_capabilities,
            reasoning=reasoning_capabilities,
            multi_agent=multi_agent_capabilities,
            human_in_the_loop=human_in_the_loop,
        )
