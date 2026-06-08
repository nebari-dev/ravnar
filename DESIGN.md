# Design: Jinja2 Sandboxing for Config Template Rendering

## Summary

Replace the unrestricted `jinja2.Environment` used in config template rendering with
`jinja2.sandbox.SandboxedEnvironment`. For **startup config rendering**, use the full `os.environ` with
`StrictUndefined` to catch typos. For **runtime agent registration** (dynamic agents via API), use a configurable
allowlist of environment variables (default empty) to prevent a privilege-escalation exfiltration attack where a user
with `agents:write` permission reads arbitrary secrets via `getCapabilities()`. The admin explicitly controls which env
vars are exposed to dynamic agents via `agents.dynamic.allowed_env_vars` in the config.

## Goals

- Prevent Jinja2 template injection from escalating to arbitrary code execution on the server.
- Limit the attack surface in the event an attacker gains write access to a config file or environment variable.
- Maintain all existing functionality for legitimate template usage (e.g., referencing `$HOME`, `$RAVNAR_*` variables in
  config values).
- Keep the change small and contained to `utils.py`.

## Non-Goals

- Removing the template rendering feature entirely — it is useful for dynamic configuration.
- Adding per-value access control (different env vars for different config values).
- Replacing Jinja2 with a different templating engine.
- Auditing each config value to determine whether it is safe to render (the concern is the renderer, not the value).

## Background / Motivation

Ravnar's configuration system applies Jinja2 template rendering to all config values via `render_template()` in
`utils.py`:

```python
def render_template(s: Any) -> Any:
    if isinstance(s, str):
        return jinja2.Environment().from_string(s).render(**os.environ)
```

This function is called as part of `RenderableMixin._render_templates`, which processes every value in the YAML config
tree and every `RAVNAR_*` environment variable. The rendered values are then used to construct the final `Config`
object.

The current code has two problems:

1. **No sandbox:** `jinja2.Environment()` is the standard environment, which allows access to Python builtins through
   the template sandbox escape technique (`{{ config.__class__.__init__.__globals__ }}`). While this requires an
   attacker to control a config value or env var, the lack of sandboxing means a single bypass of config-file protection
   leads to code execution.

2. **Runtime exfiltration via dynamic agents:** A user with `agents:write` permission can register an agent whose
   parameters contain template expressions like `{{ AWS_SECRET_ACCESS_KEY }}`. The template renders during
   `ImportStringWithParams` validation, substituting the secret into the agent's config. The agent's `getCapabilities()`
   returns this value, and the user can read it via `GET /api/agents` (requires only `agents:read`, a standard
   permission). This is a privilege escalation: a write-scoped attacker exfiltrates read-scoped secrets from the
   environment.

The fix uses `SandboxedEnvironment` for all rendering, with a restricted context only for runtime agent registration.
This blocks code execution in all cases and prevents the exfiltration attack while preserving the UX of full env-var
access for legitimate config file authoring.

## Design

### 1. Sandboxed Environment

Replace `jinja2.Environment()` with `jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined)`:

```python
import contextvars
import jinja2
import jinja2.sandbox

render_template_context: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "render_template_context", default=None
)

def render_template(s: Any, context: dict[str, str]) -> Any:
    if isinstance(s, str):
        env = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined)
        return env.from_string(s).render(**context)
    if isinstance(s, dict):
        return {render_template(k, context): render_template(v, context) for k, v in s.items()}
    if isinstance(s, list):
        return [render_template(v, context) for v in s]
    return s
```

`SandboxedEnvironment` blocks access to:

- Dunder attributes (`__class__`, `__bases__`, `__subclasses__`, `__globals__`, etc.)
- Built-in functions (`eval`, `exec`, `open`, `import`, etc.)
- Methods flagged as unsafe (`__call__` on callables that are not explicitly safe)

`StrictUndefined` raises `UndefinedError` if any variable referenced in a template is missing from the context. This
prevents silent misconfigurations caused by typos or missing env vars.

`render_template` is a pure function: it always requires an explicit `context` dict. Callers decide what context to
pass. The `render_template_context` ContextVar is used only by `ImportStringWithParams._render_template` (see below) to
inject the restricted context during runtime agent registration. When the ContextVar is not set,
`ImportStringWithParams._render_template` falls back to the full `os.environ`. This is the behavior for startup config
rendering.

This is the standard mitigation recommended by Jinja2's own documentation for security-sensitive applications. Combined
with `StrictUndefined`, it blocks code execution and prevents silent misconfigurations.

### 2. Configurable Allowlist for Runtime Agent Registration

Add `allowed_env_vars` to `DynamicAgentConfig`:

```python
class DynamicAgentConfig(BaseModel, RenderableMixin):
    enabled: bool = False
    allowed_env_vars: list[str] = Field(default_factory=list)
```

The default is an empty list: **no environment variables are exposed to dynamic agents by default**. The admin must
explicitly list each variable name that should be available in templates during runtime agent registration.

For runtime agent registration, the restricted context is constructed inline in the FastAPI dependency from the
configured allowlist. This context is **only** passed to `render_template` during `ImportStringWithParams` validation
for runtime agent registration (i.e., `RegisterAgentData`). It is **not** used for startup config rendering.

**Passing the context to runtime rendering:** `render_template` is called from Pydantic validators
(`ImportStringWithParams._render_field_templates` and `_render_param_items`), which are class methods and do not receive
request state directly. The `render_template_context` ContextVar bridges this gap. A FastAPI `yield` dependency on the
dynamic agents routes sets the context variable before request body parsing, using the `AgentHandler` instance that is
already available in the router.

```python
class AgentHandler:
    def __init__(self, agent_config: AgentConfig) -> None:
        self._static_agents: dict[str, Agent] = {id: factory() for id, factory in agent_config.static.items()}
        self._dynamic_agents: dict[str, Agent] = {}
        self._event_encoder = ag_ui.encoder.EventEncoder()
        self._dynamic_enabled = agent_config.dynamic.enabled
        self._dynamic_config = agent_config.dynamic

    def get_dynamic_render_template_context(self) -> dict[str, str]:
        allowed = self._dynamic_config.allowed_env_vars
        return {k: v for k, v in os.environ.items() if k in allowed}
```

The `_make_dynamic_agents_router` function receives `agent_handler` by closure, so it can define the `yield` dependency
inline without adding a new `Depends(get_config)`:

```python
def _make_dynamic_agents_router(
    router: schema.APIRouter,
    *,
    agent_handler: AgentHandler,
    authorized_user_with: Callable[..., Any],
) -> None:
    async def _set_restricted_template_context():
        render_template_context.set(agent_handler.get_dynamic_render_template_context())
        yield
        render_template_context.set(None)

    @router.post("", dependencies=[Depends(_set_restricted_template_context)])
    async def register_agent(
        data: RegisterAgentData,
        user: User = Depends(authorized_user_with("agents:write")),
    ) -> AgentInfo:
        agent = data.agent()
        ...
```

The `yield` dependency runs before request body parsing. `render_template_context` is set, then Pydantic validators run
and call `ImportStringWithParams._render_template`, which reads the ContextVar and receives the restricted context.
After the response is generated, the `yield` resumes and clears the context.

**Why clear the context?** FastAPI `yield` dependencies are context managers — the code after `yield` runs as cleanup.
While each request runs in its own asyncio task (so the ContextVar would naturally be garbage collected), resetting it
via `set(None)` is defensive and explicit. It prevents stale state if the task is reused (e.g., in some async frameworks
or test scenarios) and follows the `yield` dependency pattern expected by FastAPI developers. For startup config,
`render_template_context` is not set, so `ImportStringWithParams._render_template` falls back to the full `os.environ`.

**Why default-deny:** The attack model is a user with `agents:write` registering an agent that exfiltrates arbitrary env
vars via `getCapabilities()`. The admin controls the ravnar config file (or deployment configuration), so they can
explicitly decide which env vars are safe to expose to dynamic agents. A hardcoded whitelist would either be too
permissive (blocking legitimate use cases) or require code changes for every deployment. A config value lets the admin
make the security decision at deployment time.

**Note:** Because `StrictUndefined` is used, any template referencing a non-allowed env var will raise `UndefinedError`
at runtime. For conditional values, use the `default` filter (`{{ VAR | default("fallback") }}`) or the `is defined`
test (`{% if VAR is defined %}...{% endif %}`) instead of `if VAR else`, which evaluates the variable in boolean context
and triggers `UndefinedError`.

### 3. Dict Key Rendering

The function `ImportStringWithParams._render_param_items` currently calls `render_template()` on both keys and values of
the params dict:

```python
return {cls._render_template(k): cls._render_template(v) for k, v in params.items()}
```

This means env-var-derived strings can also be parameter names. With the sandboxing change this is less dangerous, but
it is still surprising behavior. The fix here is narrower: only render keys if they are strings containing template
syntax (`{{` or `{%`). A simpler approach is to apply `render_template` to all string keys as before — the sandboxing is
the real defense, and changing key-rendering behavior could break someone who relies on it (unlikely but possible).
**Decision: leave key rendering as-is, since sandboxing covers the risk.**

`ImportStringWithParams` validators call `cls._render_template`, which injects the context. The
`RenderableMixin._render_templates` validator calls `render_template` directly with the full `os.environ` context:

```python
class RenderableMixin:
    @field_validator("*", mode="before")
    @classmethod
    def _render_templates(cls, data: Any) -> Any:
        return render_template(data, context=dict(os.environ))

class ImportStringWithParams(BaseModel, Generic[T]):
    @field_validator("cls_or_fn", "params", mode="before")
    @classmethod
    def _render_field_templates(cls, f: Any) -> Any:
        if isinstance(f, str):
            return cls._render_template(f)
        return f

    @field_validator("params", mode="after")
    @classmethod
    def _render_param_items(cls, params: dict[str, Any]) -> dict[str, Any]:
        return {cls._render_template(k): cls._render_template(v) for k, v in params.items()}

    @classmethod
    def _render_template(cls, s: Any) -> Any:
        ctx = render_template_context.get()
        if ctx is None:
            ctx = dict(os.environ)
        return render_template(s, ctx)
```

During runtime agent registration, the ContextVar is set to the restricted context, so
`ImportStringWithParams._render_template` reads it and passes it to `render_template`. For startup config,
`RenderableMixin._render_templates` calls `render_template` directly with `os.environ`, bypassing the ContextVar
entirely. The `ImportStringWithParams` validators that run afterward receive plain strings (mostly no-op), but if they
do call `_render_template`, the ContextVar is not set, so they fall back to the full `os.environ`.

### 4. SecurityError Handling

`SandboxedEnvironment` raises `jinja2.exceptions.SecurityError` when a template attempts dunder access, dangerous
builtins, or other sandboxed operations. The handling depends on when the template is rendered:

**Startup config rendering** (`RenderableMixin`, `ImportStringWithParams` during startup model validation):
`SecurityError` must **propagate** (fail-closed). If a malicious or malformed template is present in the YAML config or
a `RAVNAR_*` environment variable, the server should crash at startup with a clear error. There is no legitimate reason
for a config template to trigger a `SecurityError`.

**Runtime restricted-context rendering** (`RegisterAgentData` via API, or any future feature that uses the restricted
context): `SecurityError` and `UndefinedError` must be caught and converted to a client-friendly error so the API client
receives a proper error response instead of an unhandled `500 Internal Server Error`.

Because Pydantic validators wrap every exception they catch in `ValidationError`, `HTTPException` raised from a validator
would not reach FastAPI's `HTTPException` handler. Instead, the exception is trapped inside `RequestValidationError` →
`422`. To avoid this, `ImportStringWithParams._render_template` catches `SecurityError`/`UndefinedError` and re-raises a
custom `TemplateRenderError` when `render_template_context` is set (restricted context). The custom exception carries
the relevant data (template, reason, message) so an exception handler can convert it to a `400 Bad Request` with a
generic client-facing message while preserving the full details for server-side logging.

```python
# utils.py
class TemplateRenderError(Exception):
    """Raised when template rendering fails in a restricted context."""

    def __init__(self, *, template: str, reason: str, message: str) -> None:
        self.template = template
        self.reason = reason
        self.message = message
        super().__init__(message)


class ImportStringWithParams(BaseModel, Generic[T]):
    @classmethod
    def _render_template(cls, s: Any) -> Any:
        ctx = render_template_context.get()
        if ctx is None:
            ctx = dict(os.environ)
        try:
            return render_template(s, ctx)
        except (jinja2.exceptions.SecurityError, jinja2.exceptions.UndefinedError) as e:
            if render_template_context.get() is not None:
                structlog.get_logger().warning(
                    "Template rendering blocked",
                    template=str(s),
                    reason=type(e).__name__,
                    error=str(e),
                )
                raise TemplateRenderError(
                    template=str(s),
                    reason=type(e).__name__,
                    message="Invalid configuration",
                ) from e
            raise
```

A FastAPI exception handler for `RequestValidationError` is added in `Ravnar._make_app`. It inspects the error list
and, if any error is a `value_error` whose `ctx.error` is a `TemplateRenderError`, returns a `400 Bad Request` with a
generic `{"detail": "Invalid configuration"}` response. The detailed error is logged server-side by the validator
itself with `structlog` at the `warning` level. The admin sees the detailed log message; the API client does not.

```python
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError

@app.exception_handler(RequestValidationError)
async def _template_render_validation_handler(request, exc):
    for error in exc.errors():
        if error.get("type") == "value_error":
            original = error.get("ctx", {}).get("error")
            if isinstance(original, TemplateRenderError):
                return JSONResponse(
                    status_code=400,
                    content={"detail": original.message},
                )
    return await request_validation_exception_handler(request, exc)
```

**Why this approach:**
- The `_render_template` classmethod already knows whether it's in a restricted context because `render_template_context` is set.
- It catches the exception, logs it with full context (`template`, `reason`, `error`), and replaces it with a distinct `TemplateRenderError`.
- Pydantic wraps `TemplateRenderError` in a `ValidationError` with `type: "value_error"`, but the original exception lives in `ctx.error`.
- The exception handler inspects `RequestValidationError.errors()` and looks for `TemplateRenderError` in `ctx.error`.
- If found, it returns `400` (no `422`). Otherwise, it falls back to FastAPI's default validation handler.
- The `TemplateRenderError` class has no agent-specific connotation — it is purely about template rendering — so it can be reused if other features introduce restricted-context rendering in the future.

### 5. Startup Logging

No explicit warning is added for the sandboxing change — it is a silent fix. No logging changes are required.

## Tradeoffs & Risks

- **Template features that are sandboxed:** `SandboxedEnvironment` blocks dunder access, dangerous builtins, and
  arbitrary calls. It does **not** block `range()`, `cycler()`, `joiner()`, `namespace()`, or `{% extends %}` /
  `{% include %}` — but since no template loader is configured, file-based inheritance is already non-functional. None
  of these features are used in ravnar's config templates (they are short, single-line expressions like
  `{{ HOME }}/data`).
- **StrictUndefined breakage:** `StrictUndefined` raises on any access to a missing variable. This breaks the common
  Jinja2 idiom `{{ VAR if VAR else "default" }}` because the boolean check counts as an access. The supported
  alternatives are `{{ VAR | default("default") }}` and `{% if VAR is defined %}...{% endif %}`. This is a deliberate
  breaking change for security: silent empty strings are unacceptable for config values.
- **Allowlist configuration:** The admin must explicitly configure `agents.dynamic.allowed_env_vars` for each env var
  needed by dynamic agents. This is a one-time deployment decision per variable. The default-deny model is secure but
  requires admin awareness. If a user tries to register a dynamic agent that references an unallowed env var, the
  registration fails with `UndefinedError`. The server log includes a clear message naming the undefined variable and
  suggesting the admin add it to `agents.dynamic.allowed_env_vars`. The HTTP response is generic to avoid leaking
  information.
- **Jinja2 sandbox security:** The sandbox is not perfect — sandbox escapes have been found in Jinja2 before. However,
  it raises the bar significantly. The restricted context further limits what an attacker could reach even with a
  sandbox escape.
- **Performance:** `SandboxedEnvironment` has a small overhead compared to `Environment`. Config rendering happens once
  at startup, so this is negligible.

## Testing Strategy

- **Unit tests for sandbox:**
  - Render `{{ 7 * 7 }}` → `49` (basic math still works).
  - Render `{{ HOME }}` → value of `$HOME` (env var access works).
  - Render `{{ RAVNAR_SOME_VAR }}` → value of that env var.
  - Render `{{ config.__class__ }}` → raises `SecurityError` (fail-closed at startup, converted to HTTPException at
    runtime).
  - Render `{{ self.__class__.__mro__ }}` → raises `SecurityError`.
  - Render `{{ ''.__class__.__mro__ }}` → raises `SecurityError`.
- **Unit tests for restricted context (runtime only):**
  - Env vars in `allowed_env_vars` are accessible during runtime agent registration.
  - Env vars not in `allowed_env_vars` raise `UndefinedError` during runtime agent registration.
  - Default empty allowlist: any env var reference raises `UndefinedError`.
  - `StrictUndefined` tests: `{{ VAR | default("x") }}` → `"x"`; `{% if VAR is defined %}...{% endif %}` → renders
    fallback; `{{ VAR if VAR else "x" }}` → `UndefinedError`.
- **Integration tests:**
  - Start ravnar with a config that uses `{{ HOME }}` in a path — verify the path resolves correctly.
  - Start ravnar with a config that uses an arbitrary env var (e.g., `{{ MY_SECRET }}`) — verify the path resolves
    correctly (full context is available for config).
  - Register a dynamic agent via API with `allowed_env_vars` empty — verify any env var reference fails with
    `UndefinedError`.
  - Register a dynamic agent via API with `allowed_env_vars` containing `AWS_SECRET_ACCESS_KEY` — verify the template
    renders correctly.
  - Register a dynamic agent via API with `allowed_env_vars` configured but a non-allowed env var referenced — verify it
    fails with `UndefinedError`.
- **No e2e tests needed.**

## Open Questions

_(none — all design decisions are resolved)_
