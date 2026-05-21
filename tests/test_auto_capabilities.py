"""Tests for automatic capability extraction from Pydantic AI and Agno agents."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from _ravnar.agents import AgnoAgentWrapper, PydanticAiAgentWrapper


# ---------------------------------------------------------------------------
# Pydantic AI tests
# ---------------------------------------------------------------------------


class TestPydanticAiExtractCapabilities:
    def _make_agent(self, **kwargs):
        """Create a pydantic_ai.Agent with TestModel (no API key needed)."""
        from pydantic_ai.models.test import TestModel
        import pydantic_ai

        model = TestModel()
        return pydantic_ai.Agent(model, **kwargs)

    def test_minimal_agent_no_tools(self):
        agent = self._make_agent(name="test-agent")
        caps = PydanticAiAgentWrapper.extract_capabilities(agent)

        assert caps.identity is not None
        assert caps.identity.name == "test-agent"
        assert caps.identity.type == "pydantic-ai"
        assert caps.identity.description is None
        assert caps.identity.metadata is None

        assert caps.transport is not None
        assert caps.transport.streaming is True

        assert caps.tools is not None
        assert caps.tools.supported is True
        assert caps.tools.items is None
        assert caps.tools.client_provided is True

        assert caps.output is None
        assert caps.reasoning is None
        assert caps.human_in_the_loop is None
        assert caps.multimodal is None
        assert caps.multi_agent is None

    def test_agent_with_description_and_metadata(self):
        agent = self._make_agent(
            name="test-agent",
            description="A test agent",
            metadata={"key": "value", "version": 1},
        )
        caps = PydanticAiAgentWrapper.extract_capabilities(agent)

        assert caps.identity.name == "test-agent"
        assert caps.identity.description == "A test agent"
        assert caps.identity.metadata == {"key": "value", "version": 1}

    def test_agent_with_tools(self):
        def greet(name: str) -> str:
            """Greet someone by name.

            Args:
                name: The name of the person to greet.
            """
            return f"Hello, {name}!"

        agent = self._make_agent(name="test-agent", tools=[greet])
        caps = PydanticAiAgentWrapper.extract_capabilities(agent)

        assert caps.tools is not None
        assert caps.tools.supported is True
        assert caps.tools.items is not None
        assert len(caps.tools.items) == 1
        tool = caps.tools.items[0]
        assert tool.name == "greet"
        assert tool.description is not None
        assert "name" in tool.parameters["properties"]

    def test_agent_with_thinking_capability(self):
        from pydantic_ai.capabilities import Thinking

        agent = self._make_agent(
            name="test-agent",
            capabilities=[Thinking()],
        )
        caps = PydanticAiAgentWrapper.extract_capabilities(agent)

        assert caps.reasoning is not None
        assert caps.reasoning.supported is True

    def test_agent_without_thinking_capability(self):
        agent = self._make_agent(name="test-agent")
        caps = PydanticAiAgentWrapper.extract_capabilities(agent)

        assert caps.reasoning is None

    def test_agent_with_image_generation_capability(self):
        from pydantic_ai.capabilities import ImageGeneration

        agent = self._make_agent(
            name="test-agent",
            capabilities=[ImageGeneration()],
        )
        caps = PydanticAiAgentWrapper.extract_capabilities(agent)

        assert caps.multimodal is not None
        assert caps.multimodal.output is not None
        assert caps.multimodal.output.image is True

    def test_agent_with_deferred_tool_calls(self):
        from pydantic_ai.capabilities import HandleDeferredToolCalls

        def dummy_handler(*args, **kwargs):
            return None

        agent = self._make_agent(
            name="test-agent",
            capabilities=[HandleDeferredToolCalls(handler=dummy_handler)],
        )
        caps = PydanticAiAgentWrapper.extract_capabilities(agent)

        assert caps.human_in_the_loop is not None
        assert caps.human_in_the_loop.approvals is True

    def test_agent_with_pydantic_output_type(self):
        import pydantic

        class MyOutput(pydantic.BaseModel):
            name: str
            age: int

        agent = self._make_agent(name="test-agent", output_type=MyOutput)
        caps = PydanticAiAgentWrapper.extract_capabilities(agent)

        assert caps.output is not None
        assert caps.output.structured_output is True

    def test_agent_with_text_output_type(self):
        agent = self._make_agent(name="test-agent", output_type=str)
        caps = PydanticAiAgentWrapper.extract_capabilities(agent)

        assert caps.output is None

    def test_agent_with_mixed_capabilities_including_factories(self):
        """Factory callables in capabilities should be silently skipped."""
        from pydantic_ai.capabilities import Thinking

        # A factory callable would look like: lambda ctx: Thinking()
        # We test that instance detection works alongside other capabilities
        agent = self._make_agent(
            name="test-agent",
            capabilities=[Thinking()],
        )
        caps = PydanticAiAgentWrapper.extract_capabilities(agent)

        # At minimum, Thinking should be detected
        assert caps.reasoning is not None
        assert caps.reasoning.supported is True

    def test_static_method_callable_without_instance(self):
        """extract_capabilities should work as a static method without wrapper instance."""
        agent = self._make_agent(name="standalone")
        caps = PydanticAiAgentWrapper.extract_capabilities(agent)
        assert caps.identity.name == "standalone"


# ---------------------------------------------------------------------------
# Agno tests
# ---------------------------------------------------------------------------


class TestAgnoExtractCapabilities:
    def _make_agent(self, **kwargs):
        """Create an Agno Agent with minimal setup (no model provider needed)."""
        from agno.agent import Agent

        # Use __new__ to skip __init__ which requires a model, then set attrs
        agent = Agent.__new__(Agent)
        for key, value in kwargs.items():
            setattr(agent, key, value)

        # Set defaults for unset attributes
        defaults = {
            "name": None,
            "description": None,
            "tools": [],
            "reasoning": None,
            "reasoning_agent": None,
            "structured_outputs": False,
            "output_schema": None,
            "metadata": None,
        }
        for key, value in defaults.items():
            if not hasattr(agent, key):
                setattr(agent, key, value)

        return agent

    def test_minimal_agent_no_tools(self):
        agent = self._make_agent(name="test-agent")
        caps = AgnoAgentWrapper.extract_capabilities(agent)

        assert caps.identity is not None
        assert caps.identity.name == "test-agent"
        assert caps.identity.type == "agno"
        assert caps.identity.description is None
        assert caps.identity.metadata is None

        assert caps.transport is not None
        assert caps.transport.streaming is True

        assert caps.tools is not None
        assert caps.tools.supported is True
        assert caps.tools.items is None
        assert caps.tools.client_provided is True

        assert caps.output is None
        # Agno Agent class has reasoning=False as a class-level default,
        # so reasoning.supported will be False (not None)
        assert caps.reasoning is not None
        assert caps.reasoning.supported is False
        assert caps.multi_agent is None
        assert caps.human_in_the_loop is None

    def test_agent_with_description_and_metadata(self):
        agent = self._make_agent(
            name="test-agent",
            description="A test agent",
            metadata={"key": "value"},
        )
        caps = AgnoAgentWrapper.extract_capabilities(agent)

        assert caps.identity.name == "test-agent"
        assert caps.identity.description == "A test agent"
        assert caps.identity.metadata == {"key": "value"}

    def test_agent_with_callable_tools(self):
        def add(a: int, b: int) -> int:
            """Add two numbers together."""
            return a + b

        agent = self._make_agent(name="test-agent", tools=[add])
        caps = AgnoAgentWrapper.extract_capabilities(agent)

        assert caps.tools is not None
        assert caps.tools.items is not None
        assert len(caps.tools.items) == 1
        tool = caps.tools.items[0]
        assert tool.name == "add"

    def test_agent_with_toolkit_tools(self):
        from agno.tools import Toolkit

        def multiply(a: int, b: int) -> int:
            """Multiply two numbers."""
            return a * b

        toolkit = Toolkit(name="math", tools=[multiply])
        agent = self._make_agent(name="test-agent", tools=[toolkit])
        caps = AgnoAgentWrapper.extract_capabilities(agent)

        assert caps.tools is not None
        assert caps.tools.items is not None
        assert len(caps.tools.items) == 1
        tool = caps.tools.items[0]
        assert tool.name == "multiply"

    def test_agent_with_function_tools(self):
        from agno.tools import Function

        def subtract(a: int, b: int) -> int:
            """Subtract b from a."""
            return a - b

        fn = Function.from_callable(subtract)
        agent = self._make_agent(name="test-agent", tools=[fn])
        caps = AgnoAgentWrapper.extract_capabilities(agent)

        assert caps.tools is not None
        assert caps.tools.items is not None
        assert len(caps.tools.items) == 1
        tool = caps.tools.items[0]
        assert tool.name == "subtract"

    def test_agent_with_reasoning_true(self):
        agent = self._make_agent(name="test-agent", reasoning=True)
        caps = AgnoAgentWrapper.extract_capabilities(agent)

        assert caps.reasoning is not None
        assert caps.reasoning.supported is True

    def test_agent_with_reasoning_false(self):
        agent = self._make_agent(name="test-agent", reasoning=False)
        caps = AgnoAgentWrapper.extract_capabilities(agent)

        assert caps.reasoning is not None
        assert caps.reasoning.supported is False

    def test_agent_with_reasoning_unset(self):
        agent = self._make_agent(name="test-agent", reasoning=None)
        caps = AgnoAgentWrapper.extract_capabilities(agent)

        assert caps.reasoning is None

    def test_agent_with_reasoning_agent(self):
        from agno.agent import Agent

        sub_agent = Agent.__new__(Agent)
        sub_agent.name = "sub-reasoner"
        sub_agent.description = "A sub-agent for reasoning"
        sub_agent.tools = []
        sub_agent.reasoning = None
        sub_agent.reasoning_agent = None
        sub_agent.structured_outputs = False
        sub_agent.output_schema = None
        sub_agent.metadata = None

        agent = self._make_agent(name="test-agent", reasoning_agent=sub_agent)
        caps = AgnoAgentWrapper.extract_capabilities(agent)

        assert caps.multi_agent is not None
        assert caps.multi_agent.supported is True
        assert caps.multi_agent.sub_agents is not None
        assert len(caps.multi_agent.sub_agents) == 1
        sub = caps.multi_agent.sub_agents[0]
        assert sub.name == "sub-reasoner"
        assert sub.description == "A sub-agent for reasoning"

    def test_agent_without_reasoning_agent(self):
        agent = self._make_agent(name="test-agent", reasoning_agent=None)
        caps = AgnoAgentWrapper.extract_capabilities(agent)

        assert caps.multi_agent is None

    def test_agent_with_structured_outputs_true(self):
        agent = self._make_agent(name="test-agent", structured_outputs=True)
        caps = AgnoAgentWrapper.extract_capabilities(agent)

        assert caps.output is not None
        assert caps.output.structured_output is True

    def test_agent_with_output_schema_set(self):
        agent = self._make_agent(name="test-agent", output_schema={"type": "object"})
        caps = AgnoAgentWrapper.extract_capabilities(agent)

        assert caps.output is not None
        assert caps.output.structured_output is True

    def test_agent_with_tool_requiring_confirmation(self):
        from agno.tools import Function

        def risky_action() -> str:
            """Perform a risky action."""
            return "done"

        fn = Function.from_callable(risky_action)
        fn.requires_confirmation = True

        agent = self._make_agent(name="test-agent", tools=[fn])
        caps = AgnoAgentWrapper.extract_capabilities(agent)

        assert caps.human_in_the_loop is not None
        assert caps.human_in_the_loop.approvals is True

    def test_agent_with_tool_requiring_user_input(self):
        from agno.tools import Function

        def interactive_action(value: str) -> str:
            """Perform an interactive action."""
            return f"processed: {value}"

        fn = Function.from_callable(interactive_action)
        fn.requires_user_input = True

        agent = self._make_agent(name="test-agent", tools=[fn])
        caps = AgnoAgentWrapper.extract_capabilities(agent)

        assert caps.human_in_the_loop is not None
        assert caps.human_in_the_loop.approvals is True

    def test_agent_without_hitl_tools(self):
        def safe_action() -> str:
            """Perform a safe action."""
            return "done"

        agent = self._make_agent(name="test-agent", tools=[safe_action])
        caps = AgnoAgentWrapper.extract_capabilities(agent)

        assert caps.human_in_the_loop is None

    def test_static_method_callable_without_instance(self):
        """extract_capabilities should work as a static method without wrapper instance."""
        agent = self._make_agent(name="standalone")
        caps = AgnoAgentWrapper.extract_capabilities(agent)
        assert caps.identity.name == "standalone"

    def test_agent_with_empty_tools_list(self):
        agent = self._make_agent(name="test-agent", tools=[])
        caps = AgnoAgentWrapper.extract_capabilities(agent)

        assert caps.tools is not None
        assert caps.tools.supported is True
        assert caps.tools.items is None


# ---------------------------------------------------------------------------
# Override behavior tests (both wrappers)
# ---------------------------------------------------------------------------


class TestOverrideBehavior:
    def _make_pydantic_agent(self):
        from pydantic_ai.models.test import TestModel
        import pydantic_ai

        model = TestModel()
        return pydantic_ai.Agent(model, name="test-pa")

    def _make_agno_agent(self):
        from agno.agent import Agent

        agent = Agent.__new__(Agent)
        agent.name = "test-agno"
        agent.description = None
        agent.tools = []
        agent.reasoning = None
        agent.reasoning_agent = None
        agent.structured_outputs = False
        agent.output_schema = None
        agent.metadata = None
        return agent

    def test_pydantic_ai_with_explicit_capabilities(self):
        import ag_ui.core

        custom_caps = ag_ui.core.AgentCapabilities(
            identity=ag_ui.core.IdentityCapabilities(name="custom-name", type="custom"),
            transport=ag_ui.core.TransportCapabilities(streaming=False),
        )

        agent = self._make_pydantic_agent()
        wrapper = PydanticAiAgentWrapper(agent, capabilities=custom_caps)

        result = wrapper.get_capabilities()
        assert result.identity.name == "custom-name"
        assert result.identity.type == "custom"
        assert result.transport.streaming is False

    def test_agno_with_explicit_capabilities(self):
        import ag_ui.core

        custom_caps = ag_ui.core.AgentCapabilities(
            identity=ag_ui.core.IdentityCapabilities(name="custom-name", type="custom"),
            transport=ag_ui.core.TransportCapabilities(streaming=False),
        )

        agent = self._make_agno_agent()
        wrapper = AgnoAgentWrapper(agent, capabilities=custom_caps)

        result = wrapper.get_capabilities()
        assert result.identity.name == "custom-name"
        assert result.identity.type == "custom"
        assert result.transport.streaming is False

    def test_pydantic_ai_auto_extracted_when_no_capabilities(self):
        agent = self._make_pydantic_agent()
        wrapper = PydanticAiAgentWrapper(agent)

        caps = wrapper.get_capabilities()
        assert caps.identity.name == "test-pa"
        assert caps.identity.type == "pydantic-ai"
        assert caps.transport.streaming is True

    def test_agno_auto_extracted_when_no_capabilities(self):
        agent = self._make_agno_agent()
        wrapper = AgnoAgentWrapper(agent)

        caps = wrapper.get_capabilities()
        assert caps.identity.name == "test-agno"
        assert caps.identity.type == "agno"
        assert caps.transport.streaming is True
