# %% [markdown]
# # Adding an agent to ravnar
#
# This tutorial explains how to add an agent to ravnar. You will see two approaches:
#
# 1. **Full control** — subclassing the [`Agent`][ravnar.agents.Agent] ABC directly.
# 2. **Using a wrapper** — adapting an existing [pydantic-ai](https://ai.pydantic.dev/) agent via
#    [`PydanticAiAgentWrapper`][ravnar.agents.PydanticAiAgentWrapper].
#
# A special `Client` is used for the documentation. For real-world scenarios, it can be substituted with a regular HTTP
# client with the base URL set to the URL of your ravnar deployment.

# %%
import json
import uuid
from collections.abc import AsyncIterator

from _ravnar.docs import Client


def print_json(obj):
    print(json.dumps(obj, indent=2, sort_keys=False))


def run_agent(client, agent_id: str, message: str) -> None:
    """Send a message to an agent and display the response in a human-readable format."""
    import httpx_sse

    body = {
        "thread_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "state": None,
        "tools": [],
        "context": [],
        "forwardedProps": None,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": message}],
                "id": str(uuid.uuid4()),
            }
        ],
    }

    with httpx_sse.connect_sse(client, "POST", f"/api/agents/{agent_id}/run", json=body) as event_source:
        text = ""
        for sse in event_source.iter_sse():
            event = json.loads(sse.data)
            match event["type"]:
                case "TEXT_MESSAGE_CONTENT":
                    text += event["delta"]
                case "TOOL_CALL_START":
                    print(f"  🛠  Calling tool: {event['toolCallName']}")
                case "TOOL_CALL_RESULT":
                    print(f"  ✅ {event['content']}")
                case "RUN_ERROR":
                    print(f"  ❌ Error: {event.get('error', 'Unknown error')}")

        if text:
            print(f"\n{text}")


# %% [markdown]
# ## Full control with the Agent ABC
#
# The [`Agent`][ravnar.agents.Agent] abstract base class gives you complete control over the agent's behaviour.
# You implement a single method, [`run()`][ravnar.agents.Agent.run], which receives the incoming
# `RunAgentInput` and a [`User`][ravnar.authenticators.User] object, and yields `Event`s.
#
# Let's build a simple agent that greets the current user.

# %%
import ag_ui.core

from _ravnar.agents import Agent
from _ravnar.security import User


class WhoAmIAgent(Agent):
    """A simple agent that greets the current user."""

    async def run(
        self, input: ag_ui.core.RunAgentInput, user: User
    ) -> AsyncIterator[ag_ui.core.Event]:
        message_id = str(uuid.uuid4())

        yield ag_ui.core.RunStartedEvent(
            thread_id=input.thread_id,
            run_id=input.run_id,
            parent_run_id=input.parent_run_id,
        )
        yield ag_ui.core.TextMessageStartEvent(message_id=message_id)

        text = f"Hello, {user.id}!"
        for word in text.split():
            yield ag_ui.core.TextMessageContentEvent(message_id=message_id, delta=word + " ")

        yield ag_ui.core.TextMessageEndEvent(message_id=message_id)
        yield ag_ui.core.RunFinishedEvent(thread_id=input.thread_id, run_id=input.run_id)


# %% [markdown]
# The [`User`][ravnar.authenticators.User] object carries the authenticated user's identity along with any additional data and permissions.
# When no authenticator is configured, the user defaults to the current system user, and all permissions are granted.
#
# Now we register it as a static agent through the ravnar configuration. Static agents are declared upfront and are
# available for the entire lifetime of the server.

# %%
config = {
    "agents": {
        "static": {
            "whoami": WhoAmIAgent,
        }
    }
}
client = Client(config)

# %% [markdown]
# Let's verify that our agent is registered and inspect its capabilities.

# %%
agents = client.get("/api/agents").raise_for_status().json()
print_json(agents)

# %% [markdown]
# The capabilities are derived from the default [`get_capabilities()`][ravnar.agents.Agent.get_capabilities] method of the base class which, among others,
# reports that the agent supports streaming. No tools are declared — this is a purely conversational agent.
#
# Time to send it a message.

# %%
run_agent(client, "whoami", "Who am I?")

# %% [markdown]
# The `run_agent()` helper parsed the SSE event stream and printed only the text content. The agent reads the user
# ID from the [`User`][ravnar.authenticators.User] object and includes it in the greeting.
#
# Subclassing [`Agent`][ravnar.agents.Agent] is the most flexible approach — you have full control over the event stream and can integrate
# virtually any protocol or library. However, it also means you are responsible for producing the right events at the
# right time.

# %% [markdown]
# ## Using the Pydantic AI wrapper
#
# If you already use [pydantic-ai](https://ai.pydantic.dev/), you do not need to implement the [`Agent`][ravnar.agents.Agent]
# interface yourself. ravnar ships with [`PydanticAiAgentWrapper`][ravnar.agents.PydanticAiAgentWrapper] that adapts any
# `pydantic_ai.Agent` into a ravnar agent. It handles event generation, tool call streaming, and capability detection
# automatically.
#
# Let's build a pydantic-ai agent with a `whoami` tool that accesses the authenticated user. The tool is a regular
# async function that takes [`RunContext[User]`][pydantic_ai.RunContext] as its first parameter — ravnar injects the
# [`User`][ravnar.authenticators.User] object as the dependency when the agent runs.

# %%
from pydantic_ai import RunContext


async def whoami(ctx: RunContext[User]) -> str:
    """Get the current user's identity."""
    return ctx.deps.id


# %% [markdown]
# Now we register the agent purely through the configuration — no Python instantiation needed. ravnar's
# `ImportStringWithParams` mechanism resolves nested definitions recursively, so we can declare the entire agent
# tree (model, tools, wrapper) as a single config block.

# %%
config = {
    "agents": {
        "static": {
            "pydantic-whoami": {
                "cls_or_fn": "ravnar.agents.PydanticAiAgentWrapper",
                "params": {
                    "agent": {
                        "cls_or_fn": "pydantic_ai.Agent",
                        "params": {
                            "model": {
                                "cls_or_fn": "pydantic_ai.models.test.TestModel",
                                "params": {
                                    "call_tools": "all",
                                },
                            },
                            "deps_type": User,
                            "tools": [whoami],
                        },
                    },
                },
            },
        },
    },
}
client = Client(config)

# %% [markdown]
# The wrapper automatically discovers the tool and reports it in the capabilities.

# %%
agents = client.get("/api/agents").raise_for_status().json()
print_json(agents)

# %% [markdown]
# Notice the `whoami` tool is listed with its description and an empty parameter schema (it takes no arguments).
# The [`PydanticAiAgentWrapper`][ravnar.agents.PydanticAiAgentWrapper] introspects the underlying pydantic-ai agent
# to build the capability object dynamically via
# [`extract_capabilities()`][ravnar.agents.PydanticAiAgentWrapper.extract_capabilities].
#
# Let's run it.

# %%
run_agent(client, "pydantic-whoami", "Who am I?")

# %% [markdown]
# The `TestModel` immediately invokes the `whoami` tool. The helper shows:
#
# - `🛠  Calling tool: whoami` — the tool was invoked.
# - `✅ agent` — the tool returned the user ID (your system username).
# - The final message containing the tool's output in JSON format.
#
# Because we used the config-driven approach, the entire agent lifecycle (model instantiation, tool registration,
# wrapper setup) is handled automatically. In production you would swap `TestModel` for a real model
# (e.g. `openai`, `anthropic`, `openrouter`) — everything else stays the same.

# %% [markdown]
# ## Summary
#
# - Subclass [`Agent`][ravnar.agents.Agent] directly when you need full control over the event stream or want to
#   integrate a custom protocol.
# - Use [`PydanticAiAgentWrapper`][ravnar.agents.PydanticAiAgentWrapper] when you already have a pydantic-ai agent —
#   ravnar plugs it in automatically.
# - ravnar injects the [`User`][ravnar.authenticators.User] object into the agent's `run()` method. For pydantic-ai
#   agents, it is available as `deps` in tools via `RunContext.deps`.
# - All agents are registered through the same configuration mechanism, whether they are custom subclasses or wrappers.
