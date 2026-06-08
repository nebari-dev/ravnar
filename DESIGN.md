# Design: Jinja2 Sandboxing for Config Template Rendering

## Summary

Replace the unrestricted `jinja2.Environment` used in config template rendering with `jinja2.sandbox.SandboxedEnvironment`. For **startup config rendering**, use the full `os.environ` with `StrictUndefined` to catch typos. For **runtime agent registration** (dynamic agents via API), use a restricted whitelist of environment variables to prevent a privilege-escalation exfiltration attack where a user with `agents:write` permission reads arbitrary secrets via `getCapabilities()`.

## Goals

- Prevent Jinja2 template injection from escalating to arbitrary code execution on the server.
- Limit the attack surface in the event an attacker gains write access to a config file or environment variable.
- Maintain all existing functionality for legitimate template usage (e.g., referencing `$HOME`, `$RAVNAR_*` variables in config values).
- Keep the change small and contained to `utils.py`.

## Non-Goals

- Removing the template rendering feature entirely — it is useful for dynamic configuration.
- Adding per-value access control (different env vars for different config values).
- Replacing Jinja2 with a different templating engine.
- Auditing each config value to determine whether it is safe to render (the concern is the renderer, not the value).

## Background / Motivation

Ravnar's configuration system applies Jinja2 template rendering to all config values via `render_template()` in `utils.py`:

```python
def render_template(s: Any) -> Any:
    if isinstance(s, str):
        return jinja2.Environment().from_string(s).render(**os.environ)
```

This function is called as part of `RenderableMixin._render_templates`, which processes every value in the YAML config tree and every `RAVNAR_*` environment variable. The rendered values are then used to construct the final `Config` object.

The current code has two problems:

1. **No sandbox:** `jinja2.Environment()` is the standard environment, which allows access to Python builtins through the template sandbox escape technique (`{{ config.__class__.__init__.__globals__ }}`). While this requires an attacker to control a config value or env var, the lack of sandboxing means a single bypass of config-file protection leads to code execution.

2. **Runtime exfiltration via dynamic agents:** A user with `agents:write` permission can register an agent whose parameters contain template expressions like `{{ AWS_SECRET_ACCESS_KEY }}`. The template renders during `ImportStringWithParams` validation, substituting the secret into the agent's config. The agent's `getCapabilities()` returns this value, and the user can read it via `GET /api/agents` (requires only `agents:read`, a standard permission). This is a privilege escalation: a write-scoped attacker exfiltrates read-scoped secrets from the environment.

The fix uses `SandboxedEnvironment` for all rendering, with a restricted context only for runtime agent registration. This blocks code execution in all cases and prevents the exfiltration attack while preserving the UX of full env-var access for legitimate config file authoring.

## Design

### 1. Sandboxed Environment

Replace `jinja2.Environment()` with `jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined)`:

```python
import jinja2
import jinja2.sandbox

def render_template(s: Any, *, context: dict[str, str] | None = None) -> Any:
    if isinstance(s, str):
        env = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined)
        ctx = context if context is not None else dict(os.environ)
        return env.from_string(s).render(**ctx)
    if isinstance(s, dict):
        return {render_template(k, context=context): render_template(v, context=context) for k, v in s.items()}
    if isinstance(s, list):
        return [render_template(v, context=context) for v in s]
    return s
```

`SandboxedEnvironment` blocks access to:
- Dunder attributes (`__class__`, `__bases__`, `__subclasses__`, `__globals__`, etc.)
- Built-in functions (`eval`, `exec`, `open`, `import`, etc.)
- Methods flagged as unsafe (`__call__` on callables that are not explicitly safe)

`StrictUndefined` raises `UndefinedError` if any variable referenced in a template is missing from the context. This prevents silent misconfigurations caused by typos or missing env vars.

The `context` parameter controls which environment variables are exposed to the template. When `None` (default), the full `os.environ` is used. This is the behavior for startup config rendering.

This is the standard mitigation recommended by Jinja2's own documentation for security-sensitive applications. Combined with `StrictUndefined`, it blocks code execution and prevents silent misconfigurations.

### 2. Restricted Context (Runtime Only)

For runtime agent registration, construct a restricted context dict with a whitelist of environment variables:

```python
_ALLOWED_ENV_VARS = frozenset({
    "HOME", "USER", "USERNAME",
    "HOSTNAME", "HOST",
    "PATH",
    # All RAVNAR_* variables
})

def _build_restricted_template_context() -> dict[str, str]:
    return {
        k: v for k, v in os.environ.items()
        if k in _ALLOWED_ENV_VARS or k.startswith("RAVNAR_")
    }
```

This restricted context is **only** passed to `render_template` during `ImportStringWithParams` validation for runtime agent registration (i.e., `RegisterAgentData`). It is **not** used for startup config rendering.

This ensures:
- All `RAVNAR_*` variables are available (these are the intended variables for configuring ravnar).
- Common system variables like `HOME`, `USER`, `HOSTNAME` are available (they are commonly referenced in config paths).
- All other environment variables (e.g., `AWS_*`, `DB_*`, `SECRET_*`, `PATH` — wait, `PATH` is explicitly allowed above) are not exposed.

The whitelist should be documented in the hardening guide. Users who need additional env vars in dynamic agent templates can request additions through a code change rather than via configuration (since the whitelist is a code constant, not a config item, to prevent self-defeat).

**Note:** Because `StrictUndefined` is used, any template referencing a non-whitelisted env var will raise `UndefinedError` at runtime. For conditional values, use the `default` filter (`{{ VAR | default("fallback") }}`) or the `is defined` test (`{% if VAR is defined %}...{% endif %}`) instead of `if VAR else`, which evaluates the variable in boolean context and triggers `UndefinedError`.

### 3. Dict Key Rendering

The function `ImportStringWithParams._render_param_items` currently calls `render_template()` on both keys and values of the params dict:

```python
return {render_template(k, context=context): render_template(v, context=context) for k, v in params.items()}
```

This means env-var-derived strings can also be parameter names. With the sandboxing change this is less dangerous, but it is still surprising behavior. The fix here is narrower: only render keys if they are strings containing template syntax (`{{` or `{%`). A simpler approach is to apply `render_template` to all string keys as before — the sandboxing is the real defense, and changing key-rendering behavior could break someone who relies on it (unlikely but possible). **Decision: leave key rendering as-is, since sandboxing covers the risk.**

`ImportStringWithParams._render_field_templates` and `_render_param_items` must pass the restricted context when called during runtime agent registration. For startup config rendering, `RenderableMixin._render_templates` already pre-renders values with the full `os.environ` context, so the `ImportStringWithParams` validators will receive plain strings (mostly no-op).

### 4. SecurityError Handling

`SandboxedEnvironment` raises `jinja2.exceptions.SecurityError` when a template attempts dunder access, dangerous builtins, or other sandboxed operations. The handling depends on when the template is rendered:

**Startup config rendering** (`RenderableMixin`, `ImportStringWithParams` during startup model validation): `SecurityError` must **propagate** (fail-closed). If a malicious or malformed template is present in the YAML config or a `RAVNAR_*` environment variable, the server should crash at startup with a clear error. There is no legitimate reason for a config template to trigger a `SecurityError`.

**Runtime agent registration** (`RegisterAgentData.agent` via API): `SecurityError` must be **caught and converted to an HTTPException** (e.g., `400 Bad Request` or `422 Unprocessable Entity`) so the API client receives a proper error response instead of an unhandled `500 Internal Server Error`. The error message should indicate that the template contains disallowed content.

### 5. Startup Logging

No explicit warning is added for the sandboxing change — it is a silent fix. No logging changes are required.

## Tradeoffs & Risks

- **Template features that are sandboxed:** `SandboxedEnvironment` blocks dunder access, dangerous builtins, and arbitrary calls. It does **not** block `range()`, `cycler()`, `joiner()`, `namespace()`, or `{% extends %}` / `{% include %}` — but since no template loader is configured, file-based inheritance is already non-functional. None of these features are used in ravnar's config templates (they are short, single-line expressions like `{{ HOME }}/data`).
- **StrictUndefined breakage:** `StrictUndefined` raises on any access to a missing variable. This breaks the common Jinja2 idiom `{{ VAR if VAR else "default" }}` because the boolean check counts as an access. The supported alternatives are `{{ VAR | default("default") }}` and `{% if VAR is defined %}...{% endif %}`. This is a deliberate breaking change for security: silent empty strings are unacceptable for config values.
- **Whitelist maintenance:** If a legitimate use case requires an env var not in the whitelist, a code change is needed. This is intentional — environment variables are a broad attack surface and should not be blindly exposed.
- **Jinja2 sandbox security:** The sandbox is not perfect — sandbox escapes have been found in Jinja2 before. However, it raises the bar significantly. The restricted context further limits what an attacker could reach even with a sandbox escape.
- **Performance:** `SandboxedEnvironment` has a small overhead compared to `Environment`. Config rendering happens once at startup, so this is negligible.

## Testing Strategy

- **Unit tests for sandbox:**
  - Render `{{ 7 * 7 }}` → `49` (basic math still works).
  - Render `{{ HOME }}` → value of `$HOME` (env var access works).
  - Render `{{ RAVNAR_SOME_VAR }}` → value of that env var.
  - Render `{{ config.__class__ }}` → raises `SecurityError` (fail-closed at startup, converted to HTTPException at runtime).
  - Render `{{ self.__class__.__mro__ }}` → raises `SecurityError`.
  - Render `{{ ''.__class__.__mro__ }}` → raises `SecurityError`.
- **Unit tests for restricted context (runtime only):**
  - Env vars in the whitelist are accessible during runtime agent registration.
  - Env vars not in the whitelist raise `UndefinedError` during runtime agent registration.
  - `RAVNAR_*` vars are all accessible regardless of whitelist membership.
  - `StrictUndefined` tests: `{{ VAR | default("x") }}` → `"x"`; `{% if VAR is defined %}...{% endif %}` → renders fallback; `{{ VAR if VAR else "x" }}` → `UndefinedError`.
- **Integration tests:**
  - Start ravnar with a config that uses `{{ HOME }}` in a path — verify the path resolves correctly.
  - Start ravnar with a config that uses an arbitrary env var (e.g., `{{ MY_SECRET }}`) — verify the path resolves correctly (full context is available for config).
  - Register a dynamic agent via API with a whitelisted env var in params — verify it succeeds.
  - Register a dynamic agent via API with a non-whitelisted env var in params — verify it fails with `UndefinedError` (or `SecurityError` if dunder access is attempted).
- **No e2e tests needed.**

## Open Questions

*(none — all design decisions are resolved)*
