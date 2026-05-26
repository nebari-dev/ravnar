# Design: Auto-Extract AG-UI Agent Capabilities

## Summary

Replace the minimal `AgentCapabilities` currently constructed in `PydanticAiAgentWrapper` and `AgnoAgentWrapper` (which
only sets `identity.name` and `transport.streaming`) with a comprehensive, automatically-extracted capabilities object.
Each wrapper class gains a public static `extract_capabilities(agent)` method that accepts a raw framework agent
instance and returns a fully-populated `AgentCapabilities`. The method is only called when the user does not explicitly
pass a `capabilities` argument — there is no merging. Users who want full control can pass `capabilities=` or call the
static method and mutate the result. When a capability value cannot be determined with certainty, it is left unset
(`None`) rather than set to a negative default.

## Goals

- Automatically populate AG-UI `AgentCapabilities` from `pydantic_ai.Agent` and `agno.agent.Agent` instances.
- Cover all AG-UI capability categories where source data is reliably available: `identity`, `tools`, `output`,
  `reasoning`, `multiAgent`, `humanInTheLoop`, and `multimodal` (when unambiguous).
- Zero side effects on the wrapped agent — only read operations.
- Users retain full override control: passing `capabilities=` bypasses auto-extraction entirely.

## Non-Goals

- No merging of user-provided and auto-extracted capabilities. It is one or the other.
- No runtime/dynamic capability resolution beyond calling methods on the agent instance. Tool schemas are extracted
  eagerly at wrapper construction time.
- No attempt to infer multimodal capabilities from the underlying LLM model unless the framework exposes them
  explicitly.
- No extraction from other agent frameworks (SSEAgent, DefaultAgent) — they are external to ravnar's control.

## Background / Motivation

Currently, both `PydanticAiAgentWrapper` and `AgnoAgentWrapper` construct `AgentCapabilities` with only `identity.name`
and `transport.streaming`:

```python
capabilities = ag_ui.core.AgentCapabilities(
    identity=ag_ui.core.IdentityCapabilities(name=agent.name),
    transport=ag_ui.core.TransportCapabilities(streaming=True),
)
```

This wastes rich metadata available on both agent types — tool schemas, descriptions, reasoning mode, structured output
support, sub-agents, and more. Frontend clients that consume AG-UI capabilities cannot display useful information about
agents, cannot gate features, and cannot render tool lists.

Both Agno and Pydantic AI expose structured metadata on their agent instances that maps directly to AG-UI's
`AgentCapabilities` schema.

## Design

### Public Interface

Each wrapper class gains a `@staticmethod` named `extract_capabilities()` that takes the raw framework agent instance as
its sole parameter:

```python
class PydanticAiAgentWrapper(_AgentBase):
    @staticmethod
    def extract_capabilities(agent: pydantic_ai.Agent) -> ag_ui.core.AgentCapabilities:
        ...

class AgnoAgentWrapper(_AgentBase):
    @staticmethod
    def extract_capabilities(agent: agno.agent.Agent) -> ag_ui.core.AgentCapabilities:
        ...
```

The method is static and accepts the framework agent instance directly. It does not depend on `self` or the wrapper
instance. This allows callers to use it independently — for example, to inspect a Pydantic AI agent before wrapping it,
or to use it in scripts that operate on agent instances outside of ravnar.

### Construction Logic

In `__init__`, the logic is:

```python
def __init__(self, agent, *, capabilities=None, quick_prompts=None):
    self._agent = agent
    if capabilities is None:
        capabilities = self.extract_capabilities(agent)
    super().__init__(capabilities=capabilities, quick_prompts=quick_prompts)
```

No merging occurs. If `capabilities` is provided, it is used as-is.

### Capability Categories Extracted

Both wrappers extract the following AG-UI capability categories where source data exists:

- **`identity`** — name, description, type, metadata
- **`tools`** — supported, items (name/description/parameters JSON Schema), clientProvided
- **`output`** — structuredOutput
- **`transport`** — streaming (always `True` for both)
- **`reasoning`** — supported (when the agent is configured for reasoning/thinking)
- **`multiAgent`** — supported, subAgents (when a sub-agent is configured)
- **`humanInTheLoop`** — approvals (when applicable)
- **`multimodal`** — output.image (only when unambiguously configured)

Categories not extracted (left unset): `state`, `execution`, `custom`.

### Pydantic AI Mapping

| AG-UI Capability           | Source                          | Extraction Method                                                                                                                                                                                                                                                                                                                         |
| -------------------------- | ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `identity.name`            | `agent.name`                    | Direct attribute read.                                                                                                                                                                                                                                                                                                                    |
| `identity.description`     | `agent.description`             | Direct attribute read (may be `None`).                                                                                                                                                                                                                                                                                                    |
| `identity.type`            | `"pydantic-ai"`                 | Hardcoded string literal.                                                                                                                                                                                                                                                                                                                 |
| `identity.metadata`        | N/A                           | Not extracted — pydantic_ai.Agent has no public metadata API that works without a `RunContext`. Users can set it manually via `capabilities=`.                                                                                                                                                                                                                                                          |
| `tools.supported`          | Always `True`                   | Hardcoded — Pydantic AI agents are always capable of executing tools, regardless of whether any are currently registered. This matters because AG-UI clients may supply tools at runtime.                                                                                                                                                 |
| `tools.items`              | `agent.toolsets`                | Iterate each toolset's `.tools` dict. Each `Tool` has `.name`, `.description`, and `.function_schema.json_schema` which maps to AG-UI `Tool.parameters`.                                                                                                                                                                                  |
| `tools.clientProvided`     | Always `True`                   | Hardcoded — Pydantic AI agents always accept client-provided tools at runtime. This is correct because Pydantic AI's tool resolution happens at run time, and additional tools can always be supplied when calling `Agent.run()`.                                                                                                         |
| `output.structuredOutput`  | `agent.output_type`             | If `output_type is str`, leave as `None` (plain text output). Otherwise, set to `True`.                                                                                                                                                                                                                                                   |
| `transport.streaming`      | `True`                          | Hardcoded — Pydantic AI supports streaming via `Agent.run_stream()`.                                                                                                                                                                                                                                                                      |
| `reasoning.supported`      | `agent.root_capability`         | Iterate `agent.root_capability.capabilities`. `DynamicCapability` items (factory callables) are resolved by invoking their inner function with the `RunContext`. If any resolved item is an instance of `pydantic_ai.capabilities.Thinking`, set to `True`. If no `Thinking` capability is found, leave as `None` (unset) — absence of the capability does not prove the agent lacks reasoning support. |
| `multiAgent.supported`     | N/A                             | Not extracted for Pydantic AI — sub-agent patterns are not exposed as a simple attribute on the agent instance.                                                                                                                                                                                                                           |
| `humanInTheLoop.approvals` | `agent.root_capability`         | Iterate `agent.root_capability.capabilities`. `DynamicCapability` items (factory callables) are resolved by invoking their inner function with the `RunContext`. If any resolved item is an instance of `HandleDeferredToolCalls`, set to `True`. If not found, leave as `None`. Note: deferred tools can mean either human approval or external execution; this mapping is conservative.                                                     |
| `multimodal.output.image`  | `agent.root_capability`         | Iterate `agent.root_capability.capabilities`. `DynamicCapability` items (factory callables) are resolved by invoking their inner function with the `RunContext`. If any resolved item is an instance of `ImageGeneration`, set to `True`. If not found, leave as `None`.                                                                                                                                                                      |

#### Tool extraction detail (Pydantic AI)

Tools are collected via `agent.toolsets` (public property returning a list of `AbstractToolset` subclasses). For each toolset, `await toolset.get_tools(ctx)` is called with a minimal `RunContext` (constructed from `agent.model` and a zero-initialized `RunUsage`). This uses the public API and correctly handles dynamic tool preparation. Each returned `ToolsetTool` provides a `.tool_def` (`ToolDefinition`) with:

- `tool.name` → `Tool.name`
- `tool.description` → `Tool.description`
- `tool.function_schema.json_schema` → `Tool.parameters`

This is a pre-computed dict — no async, no I/O.

### Agno Mapping

| AG-UI Capability           | Source                                                          | Extraction Method                                                                                                                                                                                                                                                                                                                                                                                                                      |
| -------------------------- | --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `identity.name`            | `agent.name`                                                    | Direct attribute read (may be `None`).                                                                                                                                                                                                                                                                                                                                                                                                 |
| `identity.description`     | `agent.description`                                             | Direct attribute read (may be `None`).                                                                                                                                                                                                                                                                                                                                                                                                 |
| `identity.type`            | `"agno"`                                                        | Hardcoded string literal.                                                                                                                                                                                                                                                                                                                                                                                                              |
| `identity.metadata`        | `agent.metadata`                                                | Direct attribute read if set (type is `dict[str, Any] \| None`). If `None`, omitted.                                                                                                                                                                                                                                                                                                                                                   |
| `tools.supported`          | Always `True`                                                   | Hardcoded — Agno agents are always capable of executing tools, regardless of whether any are currently registered. This matters because AG-UI clients may supply tools at runtime.                                                                                                                                                                                                                                                     |
| `tools.items`              | `agent.tools`                                                   | Iterate `agent.tools`. Each item is either: (1) a plain callable — wrapped via `Function.from_callable()` to get name/description/parameters; (2) a `Toolkit` — iterate `toolkit.functions` (a `dict[str, Function]`) where each `Function` has `.name`, `.description`, `.parameters`; (3) a `Function` — read `.name`, `.description`, `.parameters` directly. All items are collected into the `tools.items` list.                  |
| `tools.clientProvided`     | Always `True`                                                   | Hardcoded — Agno accepts runtime tools.                                                                                                                                                                                                                                                                                                                                                                                                |
| `output.structuredOutput`  | `agent.structured_outputs` or `agent.output_schema is not None` | True if `structured_outputs` is explicitly `True`, or if `output_schema` is set. If neither is set, leave as `None`.                                                                                                                                                                                                                                                                                                                   |
| `transport.streaming`      | `True`                                                          | Hardcoded — Agno supports streaming.                                                                                                                                                                                                                                                                                                                                                                                                   |
| `reasoning.supported`      | `agent.reasoning`                                               | Direct boolean attribute. If the attribute is set to `True` or `False`, use that value directly. Only leave as `None` if the attribute itself is absent or explicitly `None`.                                                                                                                                                                                                                                                          |
| `multiAgent.supported`     | `agent.reasoning_agent is not None`                             | True if a reasoning sub-agent is configured. If `reasoning_agent` is `None`, leave as `None`.                                                                                                                                                                                                                                                                                                                                          |
| `multiAgent.subAgents`     | `agent.reasoning_agent`                                         | If `reasoning_agent` is set, create one `SubAgentInfo` with `name=agent.reasoning_agent.name` and `description=agent.reasoning_agent.description`. If not set, leave as `None`.                                                                                                                                                                                                                                                        |
| `humanInTheLoop.approvals` | `agent.tools` (inspection)                                      | Iterate extracted tools. If any `Function` has `requires_confirmation=True` or `requires_user_input=True`, set to `True`. If none match, leave as `None`. **Open question for implementer:** verify that `requires_confirmation` and `requires_user_input` are attributes on the Agno `Function` class (or on the original tool config that survives into the extracted `Function`). If they are not, this detection needs adjustment. |
| `multimodal`               | N/A                                                             | Not extracted — Agno's model objects vary too widely by provider to reliably detect multimodal support programmatically.                                                                                                                                                                                                                                                                                                               |

#### Tool extraction detail (Agno)

The extraction handles three cases:

1. **Plain callable**: Call `Function.from_callable(callable)` which uses `inspect.signature()` and docstring parsing.
   This is microseconds — no I/O or side effects.
2. **Toolkit**: Iterate `toolkit.functions.values()` (a `dict[str, Function]`). Each `Function` already has `.name`,
   `.description`, `.parameters`.
3. **Function object**: Read `.name`, `.description`, `.parameters` directly.

The `Function.from_callable()` result has:

- `fn.name` → `Tool.name`
- `fn.description` → `Tool.description`
- `fn.parameters` → `Tool.parameters` (already a JSON Schema dict)

### File Changes

**`src/_ravnar/agents.py`**:

- Add `extract_capabilities()` static method to `PydanticAiAgentWrapper`.
- Add `extract_capabilities()` static method to `AgnoAgentWrapper`.
- Modify `__init__` on both classes to call `extract_capabilities()` when `capabilities is None`.
- Import `ag_ui.core.Tool` and `ag_ui.core.SubAgentInfo` for building tool lists and sub-agent info. The AG-UI types
  (`AgentCapabilities`, `Tool`, `SubAgentInfo`, `IdentityCapabilities`, etc.) come from the `ag-ui-protocol` package,
  available under the `ag_ui.core` import.

No new files are created.

## Tradeoffs & Risks

| Tradeoff                                            | Decision                                                         | Rationale                                                                                                                                                                                                                                                                   |
| --------------------------------------------------- | ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| No merging                                          | If user passes `capabilities`, auto-extraction is fully skipped. | Merging introduces ambiguity — which values win? Users can call `extract_capabilities()` manually and mutate the result if they want partial overrides.                                                                                                                     |
| Eager extraction                                    | Tool schemas are extracted at wrapper construction, not lazily.  | Extraction is microsecond-scale introspection. No I/O, no async, no side effects. Eager is simpler and avoids surprising latency on first `get_capabilities()` call.                                                                                                        |
| Static capability detection for Pydantic AI         | Factory callables in `capabilities` are silently skipped.        | They require a `RunContext` to resolve. Skipping is safe — the capability will still function at runtime, it just won't be reflected in `AgentCapabilities`.                                                                                                                |
| No multimodal for Agno                              | Multimodal is not attempted for Agno agents.                     | Agno model objects are heterogeneous by provider. No reliable programmatic detection exists. Users can set multimodal manually via `capabilities=`.                                                                                                                         |
| Agno `multiAgent` only covers `reasoning_agent`     | Only `reasoning_agent` is mapped to `subAgents`.                 | `reasoning_agent` is the only sub-agent mechanism on Agno agents that cleanly maps to AG-UI's `SubAgentInfo`. Team-based or delegation patterns would require more complex extraction and are out of scope. Users can override via `capabilities=`.                         |
| `_function_toolset` private attribute (Pydantic AI) | We read `agent._function_toolset` directly.                      | There is no public API to get resolved tools. This is a private attribute but is stable — it is set in `__init__` and not mutated. If Pydantic AI changes this, the extraction will fail gracefully (tools just won't appear).                                              |
| `agent.tools` direct read (Agno)                    | We read `agent.tools` directly.                                  | This is the raw tool list. It may contain unevaluated callables that `Function.from_callable()` converts. If Agno adds dynamic tool resolution that changes `agent.tools` post-construction, the extracted tools may be incomplete. Users can override via `capabilities=`. |

## Testing Strategy

### Unit Tests

Test `extract_capabilities()` on both wrapper classes with representative agent configurations:

1. **Pydantic AI**:

   - Agent with no tools, no capabilities → `tools.supported=True` (agent is capable), no tool items, minimal other
     fields.
   - Agent with tools → `tools.supported=True`, correct `tools.items` with name/description/parameters.
   - Agent with `Thinking` capability → `reasoning.supported=True`.
   - Agent with no `Thinking` capability → `reasoning.supported=None` (not `False`).
   - Agent with `ImageGeneration` capability → `multimodal.output.image=True`.
   - Agent with `HandleDeferredToolCalls` → `humanInTheLoop.approvals=True`.
   - Agent with `output_type` set to a Pydantic model → `output.structuredOutput=True`.
   - Agent with `metadata` set → `identity.metadata` populated.
   - Agent with mixed capabilities including factory callables → factories silently skipped, instances detected.

2. **Agno**:

   - Agent with no tools, `reasoning` unset → `tools.supported=True`, `reasoning.supported=None`.
   - Agent with plain callable tools → tools extracted via `Function.from_callable()`.
   - Agent with `Toolkit` tools → tools extracted from `toolkit.functions`.
   - Agent with `Function` tools → tools extracted directly.
   - Agent with `reasoning=True` → `reasoning.supported=True`.
   - Agent with `reasoning=False` → `reasoning.supported=False` (explicitly `False`, not unknown).
   - Agent with `reasoning_agent` set → `multiAgent.supported=True`, one `SubAgentInfo`.
   - Agent with `structured_outputs=True` → `output.structuredOutput=True`.
   - Agent with tool having `requires_confirmation=True` → `humanInTheLoop.approvals=True`.
   - Agent with `metadata` dict → `identity.metadata` populated.

3. **Override behavior**:
   - Both wrappers: passing `capabilities=` skips extraction entirely.
   - The user-provided `capabilities` is used as-is.

### Testing Constraints

- Tests use `pydantic_ai.models.test.TestModel` for Pydantic AI agents (no API key needed).
- Tests use `Agent.__new__(Agent)` with manually set attributes for Agno agents (no model provider needed).
- No network calls or LLM invocations required.

## Open Questions

_Resolved during implementation:_

1. **Agno HITL attribute verification** — Resolved. `requires_confirmation` and `requires_user_input` are confirmed to
   be attributes on the Agno `Function` class (see `agno.tools.function.Function.model_fields`). Both default to `None`
   and are correctly propagated when tools are extracted from both `Toolkit.functions` and `Function.from_callable()`
   results.
