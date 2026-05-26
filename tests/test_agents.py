"""Tests for automatic capability extraction from Pydantic AI and Agno agents."""

from __future__ import annotations

import ag_ui.core
import agno.agent
import compyre
import pydantic
import pydantic_ai
import pytest
from agno.tools import Function, Toolkit
from pydantic_ai.capabilities import HandleDeferredToolCalls, ImageGeneration, Thinking
from pydantic_ai.models.test import TestModel

from _ravnar.agents import AgnoAgentWrapper, PydanticAiAgentWrapper


def make_pydantic_ai_agent(**kwargs):
    return pydantic_ai.Agent(TestModel(), **kwargs)


def make_agno_agent(**kwargs):
    return agno.agent.Agent(**kwargs)


class TestPydanticAiAgentWrapper:
    async def test_explicit_capabilities(self):
        capabilities = ag_ui.core.AgentCapabilities(identity=ag_ui.core.IdentityCapabilities(name="custom-name"))

        wrapper = PydanticAiAgentWrapper(make_pydantic_ai_agent(name="agent-name"), capabilities=capabilities)
        await wrapper.setup()

        compyre.assert_equal(wrapper.get_capabilities(), capabilities)

    async def test_extracted_capabilities(self):
        name = "agent-name"

        wrapper = PydanticAiAgentWrapper(make_pydantic_ai_agent(name=name))
        await wrapper.setup()

        actual = wrapper.get_capabilities()
        expected = ag_ui.core.AgentCapabilities(
            identity=ag_ui.core.IdentityCapabilities(name=name, type="pydantic-ai"),
            transport=ag_ui.core.TransportCapabilities(streaming=True),
            tools=ag_ui.core.ToolsCapabilities(
                supported=True,
                items=[],
                client_provided=True,
            ),
            output=ag_ui.core.OutputCapabilities(structured_output=False),
        )

        compyre.assert_equal(actual, expected)


class TestPydanticAiAgentWrapperCapabilityExtraction:
    def _make_pydantic_ai_agent(self, **kwargs):
        return pydantic_ai.Agent(TestModel(), **kwargs)

    async def test_minimal_agent(self):
        name = "test-agent"

        agent = make_pydantic_ai_agent(name=name)

        actual = await PydanticAiAgentWrapper.extract_capabilities(agent)
        expected = ag_ui.core.AgentCapabilities(
            identity=ag_ui.core.IdentityCapabilities(name=name, type="pydantic-ai"),
            transport=ag_ui.core.TransportCapabilities(streaming=True),
            tools=ag_ui.core.ToolsCapabilities(
                supported=True,
                items=[],
                client_provided=True,
            ),
            output=ag_ui.core.OutputCapabilities(structured_output=False),
        )

        compyre.assert_equal(actual, expected)

    async def test_agent_with_description(self):
        name = "test-agent"
        description = "A test agent"

        agent = make_pydantic_ai_agent(name=name, description=description)
        capabilities = await PydanticAiAgentWrapper.extract_capabilities(agent)

        assert capabilities.identity.name == name
        assert capabilities.identity.description == description

    async def test_agent_with_tools(self):
        def greet(name: str) -> str:
            """Greet someone by name.

            Args:
                name: The name of the person to greet.
            """
            return f"Hello, {name}!"

        agent = make_pydantic_ai_agent(tools=[greet])
        capabilities = await PydanticAiAgentWrapper.extract_capabilities(agent)

        assert capabilities.tools is not None
        assert capabilities.tools.items is not None
        assert len(capabilities.tools.items) == 1

        tool = capabilities.tools.items[0]
        assert tool.name == "greet"
        assert tool.description is not None
        assert "name" in tool.parameters["properties"]

    async def test_agent_with_thinking_capability(self):
        agent = make_pydantic_ai_agent(capabilities=[Thinking()])
        capabilities = await PydanticAiAgentWrapper.extract_capabilities(agent)

        assert capabilities.reasoning is not None
        assert capabilities.reasoning.supported is True

    async def test_agent_with_image_generation_capability(self):
        agent = make_pydantic_ai_agent(capabilities=[ImageGeneration()])
        capabilities = await PydanticAiAgentWrapper.extract_capabilities(agent)

        assert capabilities.multimodal is not None
        assert capabilities.multimodal.output is not None
        assert capabilities.multimodal.output.image is True

    async def test_agent_with_deferred_tool_calls(self):
        def dummy_handler(*args, **kwargs):
            return None

        agent = make_pydantic_ai_agent(capabilities=[HandleDeferredToolCalls(handler=dummy_handler)])
        capabilities = await PydanticAiAgentWrapper.extract_capabilities(agent)

        assert capabilities.human_in_the_loop is not None
        assert capabilities.human_in_the_loop.approvals is True

    async def test_agent_with_factory_capabilities_skipped(self):
        def thinking_factory(ctx):
            return Thinking()

        agent = make_pydantic_ai_agent(capabilities=[thinking_factory])
        capabilities = await PydanticAiAgentWrapper.extract_capabilities(agent)

        assert capabilities.reasoning is None

    @pytest.mark.parametrize(
        "output_type,expected",
        [
            (str, False),
            (pydantic.create_model("MyOutput", name=(str, ...), age=(int, ...)), True),
        ],
    )
    async def test_agent_with_output_type(self, output_type, expected):
        agent = make_pydantic_ai_agent(output_type=output_type)
        capabilities = await PydanticAiAgentWrapper.extract_capabilities(agent)

        assert capabilities.output is not None
        assert capabilities.output.structured_output is expected


class TestAgnoAgentWrapper:
    def test_explicit_capabilities(self):
        capabilities = ag_ui.core.AgentCapabilities(identity=ag_ui.core.IdentityCapabilities(name="custom-name"))

        wrapper = AgnoAgentWrapper(make_agno_agent(name="agent-name"), capabilities=capabilities)

        compyre.assert_equal(wrapper.get_capabilities(), capabilities)

    def test_extracted_capabilities(self):
        name = "agent-name"

        wrapper = AgnoAgentWrapper(make_agno_agent(name=name))

        actual = wrapper.get_capabilities()
        expected = ag_ui.core.AgentCapabilities(
            identity=ag_ui.core.IdentityCapabilities(name=name, type="agno"),
            transport=ag_ui.core.TransportCapabilities(streaming=True),
            tools=ag_ui.core.ToolsCapabilities(
                supported=True,
                items=[],
                client_provided=False,
            ),
            reasoning=ag_ui.core.ReasoningCapabilities(supported=False),
            human_in_the_loop=ag_ui.core.HumanInTheLoopCapabilities(approvals=False),
        )

        compyre.assert_equal(actual, expected)


class TestAgnoAgentWrapperCapabilityExtraction:
    def test_minimal_agent(self):
        name = "test-agent"

        agent = make_agno_agent(name=name)

        actual = AgnoAgentWrapper.extract_capabilities(agent)
        expected = ag_ui.core.AgentCapabilities(
            identity=ag_ui.core.IdentityCapabilities(name=name, type="agno"),
            transport=ag_ui.core.TransportCapabilities(streaming=True),
            tools=ag_ui.core.ToolsCapabilities(
                supported=True,
                items=[],
                client_provided=False,
            ),
            reasoning=ag_ui.core.ReasoningCapabilities(supported=False),
            human_in_the_loop=ag_ui.core.HumanInTheLoopCapabilities(approvals=False),
        )

        compyre.assert_equal(actual, expected)

    def test_agent_with_description_and_metadata(self):
        agent = make_agno_agent(
            name="test-agent",
            description="A test agent",
            metadata={"key": "value"},
        )
        capabilities = AgnoAgentWrapper.extract_capabilities(agent)

        assert capabilities.identity.name == "test-agent"
        assert capabilities.identity.description == "A test agent"
        assert capabilities.identity.metadata == {"key": "value"}

    @pytest.mark.parametrize(
        "tools_kwarg,expected_name",
        [
            pytest.param("callable", "add"),
            pytest.param("toolkit", "multiply"),
            pytest.param("function", "subtract"),
        ],
    )
    def test_agent_with_tools_types(self, tools_kwarg, expected_name):
        def multiply(a: int, b: int) -> int:
            """Multiply two numbers."""
            return a * b

        def subtract(a: int, b: int) -> int:
            """Subtract b from a."""
            return a - b

        def add(a: int, b: int) -> int:
            """Add two numbers together."""
            return a + b

        if tools_kwarg == "toolkit":
            tools = [Toolkit(name="math", tools=[multiply])]
        elif tools_kwarg == "function":
            tools = [Function.from_callable(subtract)]
        else:
            tools = [add]

        agent = make_agno_agent(name="test-agent", tools=tools)
        capabilities = AgnoAgentWrapper.extract_capabilities(agent)

        assert capabilities.tools is not None
        assert capabilities.tools.items is not None
        assert len(capabilities.tools.items) == 1
        tool = capabilities.tools.items[0]
        assert tool.name == expected_name

    @pytest.mark.parametrize("reasoning", [True, False])
    def test_agent_with_reasoning(self, reasoning):
        agent = make_agno_agent(name="test-agent", reasoning=reasoning)
        capabilities = AgnoAgentWrapper.extract_capabilities(agent)

        assert capabilities.reasoning is not None
        assert capabilities.reasoning.supported is reasoning

    def test_agent_with_reasoning_agent(self):
        sub_agent = agno.agent.Agent(
            name="sub-reasoner",
            description="A sub-agent for reasoning",
        )
        agent = make_agno_agent(name="test-agent", reasoning_agent=sub_agent)
        capabilities = AgnoAgentWrapper.extract_capabilities(agent)

        assert capabilities.multi_agent is not None
        assert capabilities.multi_agent.supported is True
        assert capabilities.multi_agent.sub_agents is not None
        assert len(capabilities.multi_agent.sub_agents) == 1
        sub = capabilities.multi_agent.sub_agents[0]
        assert sub.name == "sub-reasoner"
        assert sub.description == "A sub-agent for reasoning"

    def test_agent_without_reasoning_agent(self):
        agent = make_agno_agent(name="test-agent", reasoning_agent=None)
        capabilities = AgnoAgentWrapper.extract_capabilities(agent)

        assert capabilities.multi_agent is None

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"structured_outputs": True},
            {"output_schema": {"type": "object"}},
        ],
    )
    def test_agent_with_structured_output(self, kwargs):
        agent = make_agno_agent(name="test-agent", **kwargs)
        capabilities = AgnoAgentWrapper.extract_capabilities(agent)

        assert capabilities.output is not None
        assert capabilities.output.structured_output is True

    @pytest.mark.parametrize(
        "hitl_kwarg",
        ["requires_confirmation", "requires_user_input"],
    )
    def test_agent_with_tool_requiring_hitl(self, hitl_kwarg):
        def risky_action() -> str:
            """Perform a risky action."""
            return "done"

        fn = Function.from_callable(risky_action)
        setattr(fn, hitl_kwarg, True)

        agent = make_agno_agent(name="test-agent", tools=[fn])
        capabilities = AgnoAgentWrapper.extract_capabilities(agent)

        assert capabilities.human_in_the_loop is not None
        assert capabilities.human_in_the_loop.approvals is True

    def test_agent_without_hitl_tools(self):
        def safe_action() -> str:
            """Perform a safe action."""
            return "done"

        agent = make_agno_agent(name="test-agent", tools=[safe_action])
        capabilities = AgnoAgentWrapper.extract_capabilities(agent)

        assert capabilities.human_in_the_loop.approvals is False

    def test_agent_with_empty_tools_list(self):
        agent = make_agno_agent(name="test-agent", tools=[])
        capabilities = AgnoAgentWrapper.extract_capabilities(agent)

        assert capabilities.tools is not None
        assert capabilities.tools.supported is True
        assert capabilities.tools.items == []
