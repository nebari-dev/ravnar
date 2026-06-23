# %% [markdown]
# # Adding an agent to ravnar
#
# This tutorial explains how to add an agent to ravnar. You will see two approaches:
#
# 1. **Full control** — subclassing the `Agent` ABC directly.
# 2. **Using a wrapper** — adapting an existing [pydantic-ai](https://ai.pydantic.dev/) agent via
#    `PydanticAiAgentWrapper`.
#
# Building on the wrapper, the final section shows how to give an agent the tools of an
# [MCP](https://modelcontextprotocol.io/) server.
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
# The `Agent` abstract base class gives you complete control over the agent's behaviour.
# You implement a single method, `run()`, which receives the incoming
# `RunAgentInput` and a `User` object, and yields `Event`s.
#
# Let's build a simple agent that greets the current user.

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
# The capabilities are derived from the default `get_capabilities()` method of the base class which, among others,
# reports that the agent supports streaming. No tools are declared — this is a purely conversational agent.
#
# Time to send it a message.

# %%
run_agent(client, "whoami", "Who am I?")

# %% [markdown]
# The `run_agent()` helper parsed the SSE event stream and printed only the text content. The agent reads the user
# ID from the `User` object and includes it in the greeting.
#
# Subclassing `Agent` is the most flexible approach — you have full control over the event stream and can integrate
# virtually any protocol or library. However, it also means you are responsible for producing the right events at the
# right time.

# %% [markdown]
# ## Using the Pydantic AI wrapper
#
# If you already use [pydantic-ai](https://ai.pydantic.dev/), you do not need to implement the `Agent`
# interface yourself. ravnar ships with `PydanticAiAgentWrapper` that adapts any
# `pydantic_ai.Agent` into a ravnar agent. It handles event generation, tool call streaming, and capability detection
# automatically.
#
# Let's build a pydantic-ai agent with a `whoami` tool that accesses the authenticated user. The tool is a regular
# async function that takes `RunContext[User]` as its first parameter — ravnar injects the
# `User` object as the dependency when the agent runs.

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
# The `PydanticAiAgentWrapper` introspects the underlying pydantic-ai agent
# to build the capability object dynamically via
# `extract_capabilities()`.
#
# Let's run it.

# %%
run_agent(client, "pydantic-whoami", "Who am I?")

# %% [markdown]
# `TestModel` is pydantic-ai's stand-in for a real model: it exercises the full agent plumbing without calling an
# LLM, and with `call_tools="all"` it invokes every available tool once. The helper shows:
#
# - `🛠  Calling tool: whoami` — the tool was invoked.
# - `✅ agent` — the value returned by the tool, i.e. the user ID. Since no authenticator is configured, this is your
#   system username.
# - The final assistant message, which `TestModel` builds by echoing the tool's output as JSON.
#
# Because we used the config-driven approach, the entire agent lifecycle (model instantiation, tool registration,
# wrapper setup) is handled automatically. In production you would swap `TestModel` for a real model
# (e.g. `openai`, `anthropic`, `openrouter`) — everything else stays the same.

# %% [markdown]
# ## Connecting an MCP server
#
# [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) servers expose tools, resources, and prompts over
# a standard protocol, so any MCP-compatible client can use them. pydantic-ai connects to an MCP server with
# [`MCPToolset`](https://ai.pydantic.dev/mcp/client/), and because the wrapper introspects the agent's toolsets, the
# server's tools are discovered and become callable exactly like the in-process `whoami` tool above — no extra wiring
# on the ravnar side.
#
# One difference matters: an MCP server runs as a *separate process* (or a remote service), so its tools cannot access
# ravnar's injected `User`. Reach for an in-process pydantic-ai tool when a tool needs
# the caller's identity, and for an MCP server when the capability is self-contained.
#
# Let's write a minimal stdio MCP server that exposes a single `add` tool. In a real project this would be a separate
# service; here we write it to a temporary file so the tutorial stays self-contained.

# %%
import pathlib
import tempfile

mcp_server = pathlib.Path(tempfile.mkdtemp()) / "calculator.py"
mcp_server.write_text(
    '''
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("calculator")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


if __name__ == "__main__":
    mcp.run()
'''
)

# %% [markdown]
# Now we declare an agent that connects to it. [`MCPToolset`](https://ai.pydantic.dev/mcp/client/) accepts the path to
# a Python script and launches it as a stdio server (it also accepts a URL for a remote HTTP/SSE server). We add it to
# the agent's `toolsets` through the same recursive config mechanism — no Python instantiation needed.

# %%
config = {
    "agents": {
        "static": {
            "calculator": {
                "cls_or_fn": "ravnar.agents.PydanticAiAgentWrapper",
                "params": {
                    "agent": {
                        "cls_or_fn": "pydantic_ai.Agent",
                        "params": {
                            "model": {
                                "cls_or_fn": "pydantic_ai.models.test.TestModel",
                                "params": {"call_tools": "all"},
                            },
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
# During setup the wrapper connects to the MCP server and discovers its tools. The `add` tool appears in the
# capabilities — this time *with* a parameter schema, unlike the argument-less `whoami`.

# %%
agents = client.get("/api/agents").raise_for_status().json()
print_json(agents)

# %% [markdown]
# Let's run it.

# %%
run_agent(client, "calculator", "What is 2 + 3?")

# %% [markdown]
# `TestModel` calls `add` with placeholder arguments (`0` and `0`), so the result is `0` rather than `5` — it does
# not read the operands from the message. A real model would call `add(a=2, b=3)` and get `5`. The point of the
# example is the plumbing: ravnar connected to the MCP server, advertised its tools, and routed the tool call to it
# with no code changes — only configuration.
#
# To connect to a server you do not run yourself (the common case), pass its URL instead of a script path, e.g.
# `{"cls_or_fn": "pydantic_ai.mcp.MCPToolset", "params": {"client": "https://example.com/mcp"}}`.

# %% [markdown]
# ## Summary
#
# - Subclass `Agent` directly when you need full control over the event stream or want to
#   integrate a custom protocol.
# - Use `PydanticAiAgentWrapper` when you already have a pydantic-ai agent —
#   ravnar plugs it in automatically.
# - ravnar injects the `User` object into the agent's `run()` method. For pydantic-ai
#   agents, it is available as `deps` in tools via `RunContext.deps`.
# - Add [`MCPToolset`](https://ai.pydantic.dev/mcp/client/) to a pydantic-ai agent's `toolsets` to expose the tools of
#   any MCP server; the wrapper discovers and streams them automatically.
# - All agents are registered through the same configuration mechanism, whether they are custom subclasses or wrappers.
