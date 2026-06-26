# %% [markdown]
# # Configuring an agent
#
# This tutorial explains how to configure an agent in ravnar. You will see two approaches:
#
# 1. **Full control** — subclassing the [`Agent`][ravnar.agents.Agent] ABC directly.
# 2. **Using a wrapper** — adapting an existing [pydantic-ai](https://ai.pydantic.dev/) agent via
#    [`PydanticAiAgentWrapper`][ravnar.agents.PydanticAiAgentWrapper], and giving it the tools of an
#    [MCP](https://modelcontextprotocol.io/) server.
#
# pydantic-ai is used throughout as the example, but ravnar is not tied to it: under the hood ravnar is an AG-UI
# server, so any agent that emits AG-UI events is a first-class citizen. Other built-in agents are covered at the
# end.
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

    with httpx_sse.connect_sse(
        client, "POST", f"/api/agents/{agent_id}/run", json=body
    ) as event_source:
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
# ravnar is, at its core, an [AG-UI](https://ag-ui.com) server: every agent talks to clients by emitting a stream of
# AG-UI events — `RUN_STARTED`, `TEXT_MESSAGE_CONTENT`, `TOOL_CALL_START`, `RUN_FINISHED`, and so on. ravnar itself
# doesn't decide what those events are; it streams whatever the agent produces to the client over Server-Sent Events.
# Every integration in this tutorial is ultimately just a different way of producing that event stream.
#
# The [`Agent`][ravnar.agents.Agent] abstract base class is the most direct way to produce it, giving you complete
# control over the agent's behaviour. You implement a single method, [`run()`][ravnar.agents.Agent.run], which receives the incoming
# `RunAgentInput` (the AG-UI request — messages, thread and run IDs, and any client-supplied tools and state) and the
# authenticated [`User`][ravnar.authenticators.User], and yields the `ag_ui.core.Event`s that make up the response.
#
# Let's build a simple agent that greets the current user. The sequence below — start the run, open a text message,
# stream its content word by word, end the message, finish the run — is the canonical shape of an AG-UI text response.

# %%
import ag_ui.core

from ravnar.agents import Agent
from ravnar.authenticators import User


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
            yield ag_ui.core.TextMessageContentEvent(
                message_id=message_id, delta=word + " "
            )

        yield ag_ui.core.TextMessageEndEvent(message_id=message_id)
        yield ag_ui.core.RunFinishedEvent(
            thread_id=input.thread_id, run_id=input.run_id
        )


# %% [markdown]
# The `User` object carries the authenticated user's identity along with any additional data and permissions.
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
# The capabilities are derived from the default [`get_capabilities()`][ravnar.agents.Agent.get_capabilities] method of
# the base class which, among others, reports that the agent supports streaming. No tools are declared — this is a
# purely conversational agent.
#
# Time to send it a message.

# %%
run_agent(client, "whoami", "Hello!")

# %% [markdown]
# The `run_agent()` helper parsed the SSE event stream and printed only the text content. The agent reads the user
# ID from the `User` object and includes it in the greeting.
#
# Subclassing [`Agent`][ravnar.agents.Agent] is the most flexible approach — you have full control over the event
# stream and can integrate virtually any protocol or library. However, it also means you are responsible for producing
# the right events at the right time.

# %% [markdown]
# ## Using the Pydantic AI wrapper
#
# If you already use [pydantic-ai](https://ai.pydantic.dev/), you do not need to implement the
# [`Agent`][ravnar.agents.Agent] interface yourself. ravnar ships with
# [`PydanticAiAgentWrapper`][ravnar.agents.PydanticAiAgentWrapper], which adapts any `pydantic_ai.Agent` into a ravnar
# agent — handling AG-UI event generation, tool call streaming, and capability detection automatically.
#
# We'll give a single agent two kinds of tool: an in-process Python function, and the tools of an
# [MCP](https://modelcontextprotocol.io/) server.
#
# First, the in-process tool. `whoami` is a regular async function that takes
# [`RunContext`](https://ai.pydantic.dev/api/tools/#pydantic_ai.tools.RunContext)`[`[`User`][ravnar.authenticators.User]`]`
# as its first parameter — ravnar injects the authenticated `User` as the dependency when the agent runs.

# %%
from pydantic_ai import RunContext


async def whoami(ctx: RunContext[User]) -> str:
    """Get the current user's identity."""
    return ctx.deps.id


# %% [markdown]
# Second, an MCP server. pydantic-ai connects to one with [`MCPToolset`](https://ai.pydantic.dev/mcp/client/), and
# because the wrapper introspects the agent's `toolsets`, the server's tools are discovered and called exactly like
# `whoami` — no extra wiring on the ravnar side.
#
# One difference matters: an MCP server runs as a *separate process* (or a remote service), so its tools don't
# automatically receive ravnar's injected `User` the way an in-process tool does — though you can forward it
# explicitly if needed. Reach for an in-process tool when a tool needs the caller's identity, and an MCP server when
# the capability is self-contained.
#
# Let's write a minimal stdio MCP server that exposes a single `greet` tool. In a real project this would be a
# separate service; here we write it to a temporary file so the tutorial stays self-contained.

# %%
import pathlib
import tempfile

mcp_server = pathlib.Path(tempfile.mkdtemp()) / "greeter.py"
mcp_server.write_text(
    '''
from fastmcp import FastMCP

mcp = FastMCP("greeter")


@mcp.tool()
def greet() -> str:
    """Return a friendly greeting from the MCP server."""
    return "Hello from the MCP server!"


if __name__ == "__main__":
    mcp.run()
'''
)

# %% [markdown]
# Now we register the agent purely through configuration — no Python instantiation needed. ravnar's
# [configuration import mechanism](../../references/config/#plugins) resolves nested `cls_or_fn`/`params` definitions
# recursively, so we declare the whole agent tree — model, wrapper, the in-process `tools`, and the MCP `toolsets` —
# as a single config block. [`MCPToolset`](https://ai.pydantic.dev/mcp/client/) takes the path to a script and
# launches it as a stdio server (it also accepts a URL for a remote HTTP/SSE server).

# %%
config = {
    "agents": {
        "static": {
            "assistant": {
                "cls_or_fn": "ravnar.agents.PydanticAiAgentWrapper",
                "params": {
                    "agent": {
                        "cls_or_fn": "pydantic_ai.Agent",
                        "params": {
                            "model": {
                                "cls_or_fn": "pydantic_ai.models.test.TestModel",
                                "params": {"call_tools": "all"},
                            },
                            "deps_type": User,
                            "tools": [whoami],
                            "toolsets": [
                                {
                                    "cls_or_fn": "pydantic_ai.mcp.MCPToolset",
                                    "params": {"client": str(mcp_server)},
                                }
                            ],
                        },
                    },
                },
            },
        },
    },
}
client = Client(config)

# %% [markdown]
# The wrapper builds the capability object dynamically by introspecting the underlying pydantic-ai agent (via
# [`extract_capabilities()`][ravnar.agents.PydanticAiAgentWrapper.extract_capabilities]), so both tools show up: the in-process `whoami`, and `greet`, whose definition — name,
# description, and (empty) parameter schema — was discovered from the MCP server.

# %%
agents = client.get("/api/agents").raise_for_status().json()
print_json(agents)

# %% [markdown]
# Let's run it. [`TestModel`](https://ai.pydantic.dev/api/models/test/#pydantic_ai.models.test.TestModel) is
# pydantic-ai's stand-in for a real model: it exercises the full agent plumbing without
# calling an LLM, and with `call_tools="all"` it invokes every available tool once — both the in-process `whoami` and
# the MCP `greet`.

# %%
run_agent(client, "assistant", "Hello!")

# %% [markdown]
# Both tools take no arguments, so
# [`TestModel`](https://ai.pydantic.dev/api/models/test/#pydantic_ai.models.test.TestModel) calls them exactly as a
# real model would and the results are real:
# `whoami` returns your system username (no authenticator is configured) and `greet` returns the MCP server's
# greeting. The point is the plumbing: ravnar exposed an in-process tool and an MCP server's tool through the *same*
# wrapper, advertised both, and routed the calls — all from configuration. In production you would swap
# [`TestModel`](https://ai.pydantic.dev/api/models/test/#pydantic_ai.models.test.TestModel)
# for a real model (e.g. `openai`, `anthropic`, `openrouter`), which would decide when to call each tool based on the
# conversation; everything else stays the same.
#
# To use a server you do not run yourself (the common case), pass its URL instead of a script path:
#
# ```python
# {"cls_or_fn": "pydantic_ai.mcp.MCPToolset", "params": {"client": "https://example.com/mcp"}}
# ```

# %% [markdown]
# ## Other built-in agents
#
# pydantic-ai is just one option. ravnar also ships [`AgnoAgentWrapper`][ravnar.agents.AgnoAgentWrapper] for
# [Agno](https://docs.agno.com/) agents and [`SSEAgent`][ravnar.agents.SSEAgent] to connect any agent that already
# speaks AG-UI over HTTP. You can also implement the [`Agent`][ravnar.agents.Agent] ABC directly, as in the first
# section. See the [Python API reference](../../references/python_api/#ravnar.agents) for the full list of built-in
# agents.

# %% [markdown]
# ## Summary
#
# - ravnar is an AG-UI server: an agent is anything that produces a stream of AG-UI events, however you choose to
#   build it.
# - Subclass [`Agent`][ravnar.agents.Agent] directly when you need full control over the event stream or want to
#   integrate a custom protocol.
# - Use [`PydanticAiAgentWrapper`][ravnar.agents.PydanticAiAgentWrapper] when you already have a pydantic-ai agent —
#   ravnar plugs it in automatically.
# - ravnar injects the [`User`][ravnar.authenticators.User] object into the agent's `run()` method. For pydantic-ai
#   agents, it is available as `deps` in tools via
#   [`RunContext`](https://ai.pydantic.dev/api/tools/#pydantic_ai.tools.RunContext)`.deps`.
# - Add [`MCPToolset`](https://ai.pydantic.dev/mcp/client/) to a pydantic-ai agent's `toolsets` to expose the tools of
#   any MCP server; the wrapper discovers and streams them automatically.
# - All agents are registered through the same configuration mechanism, whether they are custom subclasses or wrappers.
