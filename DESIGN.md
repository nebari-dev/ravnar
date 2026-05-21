# Design: Task-Level Authorization (RBAC)

## Summary

Add task-level authorization to ravnar's API by introducing a flat list of permissions on the `schema.User` model. Permissions are ephemeral, provided exclusively by the Authenticator on each request, and never stored by ravnar. A new factory dependency `authorized_user_with(*permissions)` gates each endpoint, checking that the authenticated user possesses all required permissions. A `require_permissions` decorator provides the same check for non-endpoint functions. Existing item-level authorization (user-scoped data isolation) is untouched.

## Goals

- Gate every API endpoint behind task-level authorization using a permission system.
- Keep permissions ephemeral — sourced from the Authenticator, never persisted by ravnar.
- Make authz transparent to endpoint logic via a `Depends()` factory.
- Provide a `require_permissions` decorator for internal functions that need permission checks without being FastAPI endpoints.
- Preserve existing item-level authorization (user sees only their own threads, files, etc.).
- Require no database changes, no admin interface, and no user management features.

## Non-Goals

- Role hierarchy or role-to-permission mapping inside ravnar. The Authenticator handles any role→permission translation externally.
- Persistent user management, role assignment, or admin APIs.
- Changing item-level authorization (user-scoped data isolation on DB queries).
- Breaking backwards compatibility — this is an alpha release and BC is explicitly a non-goal.

## Background / Motivation

Currently ravnar only authenticates users and enforces item-level authorization (e.g., a user can only see their own threads). There is no task-level authorization: any authenticated user can perform any action. This design adds permission-based access control at the task level — e.g., "can this user create a thread?", "can this user upload a file?" — while leaving item-level scoping intact.

## Design

### Permission Model

A permission is a validated string with format `<resource>:<action>`.

The permission registry is a module-level dictionary in `auth.py`:

```python
PERMISSION_REGISTRY: dict[str, list[str]] = {
    "files": ["read", "write", "delete"],
    "threads": ["read", "write", "delete"],
    "agents": ["read", "write", "delete"],
}
```

The `Permission` type validates that both the resource and action exist in this registry. Unknown resources or actions raise `ValueError`. Adding a new permission requires updating the registry.

`ALL_PERMISSIONS` is a module-level `frozenset` derived from the registry, representing the complete set of all valid permissions.

### Permission Taxonomy

| Permission | Endpoints |
|---|---|
| `files:read` | `GET /api/files/{id}`, `GET /api/files/{id}/content` |
| `files:write` | `POST /api/files`, `POST /api/threads/{id}/run` (file hydration) |
| `files:delete` | `DELETE /api/files/{id}` |
| `threads:read` | `GET /api/threads`, `GET /api/threads/{id}`, `GET /api/threads/{id}/messages` |
| `threads:write` | `POST /api/threads`, `POST /api/threads/{id}/run`, `POST /api/threads/{id}/rename` |
| `threads:delete` | `DELETE /api/threads`, `DELETE /api/threads/{id}` |
| `agents:read` | `GET /api/agents`, `POST /api/agents/{id}/run` |
| `agents:write` | `POST /api/agents` (register agent, dynamic only) |
| `agents:delete` | `DELETE /api/agents/{id}` (unregister agent, dynamic only) |

Endpoints that require authentication but no specific permission use `authorized_user_with()` with no arguments:
- `GET /api/user`
- `GET /api/config`

### New Module: `auth.py`

A new internal module at `src/_ravnar/auth.py` houses the `User` model, `Permission` type, permission registry, `ALL_PERMISSIONS` constant, and the authorization factory.

#### Models and Types (moved from `schema/misc.py`)

- **`User`** — moved from `schema/misc.py` to `auth.py`. `schema/__init__.py` imports it from `auth.py` instead. This avoids circular imports because `auth.py` only needs `BaseModel` from `schema/misc.py`, which has no dependency on `User`.
- **`Permission`** — `Annotated[str, AfterValidator(...)]` that validates against the permission registry.

#### Constants

- **`PERMISSION_REGISTRY`** — `{resource: [actions]}` dictionary defining all valid permissions.
- **`ALL_PERMISSIONS`** — `frozenset` of all valid permissions derived from the registry. Used as the default permission set for unauthenticated environments and the debug authenticator.

#### Core Permission-Check Logic

A shared internal function performs the actual permission check:

```python
def _check_permissions(user: schema.User, required: frozenset[str]) -> None:
    """Raise HTTPException(403) if user lacks any required permission."""
```

This is used by both the factory and the decorator to avoid duplication.

#### `make_authorized_user_factory`

```python
def make_authorized_user_factory(
    security_config: SecurityConfig,
) -> tuple[Callable[..., Awaitable[schema.User]], Callable[..., Any], Callable[..., Callable]]:
    """Returns (authenticated_user, authorized_user_with, require_permissions)."""
```

The function:

1. **Creates `authenticated_user`** from `security_config.authenticator`:
   - If no authenticator is configured, returns a default user with **`ALL_PERMISSIONS`** (full admin).
   - If configured, instantiates the authenticator and resolves forward references on its `authenticate` method.

2. **Returns `authorized_user_with`** — a factory that takes `*permissions: str` and returns a FastAPI dependency:
   - The returned dependency accepts `user: schema.User = Depends(authenticated_user)` and checks that all required permissions are present in `user.permissions`.
   - If no permissions are passed (`authorized_user_with()`), it only authenticates the user without any permission gate.
   - Missing permissions raise `HTTPException(status_code=403, detail="Insufficient permissions")`.

3. **Returns `require_permissions`** — a decorator that wraps a function (sync or async) and checks permissions before executing:
   - The wrapper inspects the wrapped function's kwargs for a `user: schema.User` parameter.
   - Delegates to `_check_permissions` (same core logic as the factory).
   - Raises `HTTPException(status_code=403, detail="Insufficient permissions")` on failure.
   - Used on internal functions like `hydrate_files` that need permission checks without being FastAPI endpoints.

The `authenticated_user` reference is captured in the closure of `authorized_user_with`, so FastAPI deduplicates the auth call across multiple `Depends()` invocations in the same request.

This module is only imported from `core.py`. The `authorized_user_with` factory and `require_permissions` decorator are then passed to router factories.

### Endpoint Dependency Pattern

Every endpoint replaces `user: schema.User = Depends(authenticated_user)` with:

```python
user: schema.User = Depends(authorized_user_with("files:read"))
```

The factory returns a dependency that:
1. Delegates to `authenticated_user` for authentication.
2. Checks that the user's permission set contains all specified permissions.
3. Returns the user object.

Endpoints that need authentication but no permission check use:

```python
user: schema.User = Depends(authorized_user_with())
```

#### `require_permissions` Decorator

For internal functions that are not FastAPI endpoints (e.g., `hydrate_files` in `threads.py`), the `@require_permissions("files:read", "files:write")` decorator provides the same permission check. The wrapped function must accept a `user: schema.User` kwarg.

### Router-Level Dependencies

The top-level API router (`src/_ravnar/api/__init__.py`) retains a router-level auth-only dependency:

```python
router = schema.APIRouter(tags=["API"], dependencies=[Depends(authorized_user_with())])
```

This ensures every request to `/api/*` passes through authentication, even if an endpoint accidentally omits its `Depends()` declaration.

Router-level dependencies are **removed** from all sub-routers (`threads.py`, `files.py`, `agents.py`). Per-endpoint `Depends(authorized_user_with(...))` declarations are the sole source of permission checks, making the required permissions visible in each endpoint's signature.

### `core.py` Changes

`core.py` becomes the single caller of `make_authorized_user_factory`:

```python
authenticated_user, authorized_user_with, require_permissions = make_authorized_user_factory(config.security)
```

The `authorized_user_with` factory and `require_permissions` decorator are passed to `make_api_router()` and through to sub-routers.

### Schema Changes

#### `schema.User` (moved to `auth.py`)

```python
class User(BaseModel):
    id: str
    data: dict[str, Any] = Field(default_factory=dict)
    permissions: list[Permission] = Field(default_factory=list)
```

A validator on the `permissions` field deduplicates entries and sorts them alphabetically.

#### `Permission` Type

```python
Permission = Annotated[
    str,
    AfterValidator(_validate_permission),
]
```

The validator checks:
1. Format: exactly one colon separator, non-empty resource and action parts.
2. Resource exists in `PERMISSION_REGISTRY`.
3. Action exists in `PERMISSION_REGISTRY[resource]`.

Any violation raises `ValueError`.

### Authenticator Changes

#### `DebugAuthenticator`

Returns a user with **`ALL_PERMISSIONS`** (acts as an admin):

```python
return schema.User(
    id="debug",
    permissions=list(ALL_PERMISSIONS),
    data={...},
)
```

Uses the centralized `ALL_PERMISSIONS` constant so the permission list is never manually duplicated.

#### `ForwardedUserAuthenticator`

Add a configurable header for permissions:

```python
class ForwardedUserAuthenticator(Authenticator):
    def __init__(
        self,
        *,
        id_header: str = "X-Forwarded-User",
        permissions_header: str = "X-Forwarded-Permissions",
    ):
        ...
```

The permissions header contains a comma-separated list (e.g., `files:read,threads:write`). Missing header → empty permissions.

#### `BearerTokenAuthenticator` / `OIDCTokenValidator`

`OIDCTokenValidator` gains an optional parameter:

```python
class OIDCTokenValidator:
    def __init__(
        self,
        *,
        issuer: str,
        algorithms: list[str] | None = None,
        audience: str | None = None,
        permissions_claim: str | None = None,  # NEW
    ):
```

- If `permissions_claim` is **not set** (default): permissions are empty. User can only access endpoints requiring no specific permission (`GET /api/user`, `GET /api/config`).
- If `permissions_claim` **is set** and present in the JWT: extract the claim value (must be a list of strings) as the user's permissions.
- If `permissions_claim` **is set** but missing from the JWT payload: raise `HTTPException(status_code=401, detail="Required permissions claim missing in token")`.

`BearerTokenAuthenticator` requires **no changes** — it is a thin wrapper around whatever `TokenValidator` is passed to it. The `permissions_claim` parameter lives entirely in `OIDCTokenValidator.__init__` and is configured directly in the admin's config (e.g., YAML) when constructing the validator.

### Config Changes

No changes to `SecurityConfig`. Authenticators are configured via `ImportStringWithParams`, so new parameters (`permissions_header`, `permissions_claim`) are provided through config YAML or environment variables.

### File Hydration Permission Check

The `hydrate_files` function in `threads.py` requires `files:read` and `files:write` but only when file content is actually present in the run messages. Rather than gating the entire `POST /api/threads/{id}/run` endpoint with these permissions (which would block users who never use files), the `@require_permissions("files:read", "files:write")` decorator is applied directly to `hydrate_files`.

### `GET /api/user` Response

The endpoint returns `schema.User` as-is, which now includes the `permissions` list. Clients can inspect what they are allowed to do.

## Tradeoffs & Risks

| Tradeoff | Explanation | Mitigation |
|---|---|---|
| Per-endpoint declarations are mandatory | Removing router-level dependencies from sub-routers means an endpoint without `Depends(authorized_user_with(...))` is unprotected (except by the top-level auth guard). | Top-level auth still blocks unauthenticated access. Code review and tests catch missing declarations. |
| Flat permissions, no roles | The Authenticator must map roles to permissions externally. Ravnar has no concept of "admin" or "editor" roles. | Acceptable — role semantics belong to the identity provider, not ravnar. |
| Strict permission validation | Adding a new resource or action requires updating the permission registry. | Centralized registry makes this a one-line change. The tradeoff is acceptable because ravnar defines the permission taxonomy, not the admin. |
| `/api/config` remains authenticated | The top-level router dependency means even config requires authentication. | This is intentional — the current code already does this. |

## Testing Strategy

### Unit Tests

- **`Permission` validator**: Valid formats accepted, invalid formats rejected, container dedup/sort verified, unknown resource/action rejected.
- **`authorized_user_with` factory**:
  - User with all required permissions → returns user.
  - User missing one permission → 403.
  - User with no permissions calling `authorized_user_with()` (no args) → returns user.
  - Empty permission check with non-empty user permissions → returns user.
- **`require_permissions` decorator**:
  - Decorated function with correct permissions → executes normally.
  - Decorated function with missing permissions → 403, function body not executed.
- **Authenticator changes**:
  - `DebugAuthenticator` returns full permission set.
  - `ForwardedUserAuthenticator` parses permissions from header, handles missing header.
  - `OIDCTokenValidator` with `permissions_claim`: extracts claim when present, raises 401 when claim is configured but missing from JWT, returns empty when not configured.

### Integration Tests

- **Permission-gated endpoints**: Test each endpoint with a user that has the correct permission (200) and without it (403).
- **Auth-only endpoints**: `GET /api/user` and `GET /api/config` work with any authenticated user, even with no permissions.
- **File hydration in runs**: Verify that `POST /api/threads/{id}/run` with file content requires `files:read` and `files:write` via the `@require_permissions` decorator.

### Test Fixtures

Extend `make_app_client` or the test conftest to support injecting users with specific permission sets. The `DebugAuthenticator` is already used in tests and will return all permissions, so existing tests should continue to pass. For negative tests, a custom authenticator or mock is needed.

## Open Questions

_No unresolved questions._
