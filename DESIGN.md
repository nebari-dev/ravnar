# Design: Task-Level Authorization (RBAC)

## Summary

Add task-level authorization to ravnar's API by introducing a flat list of permissions on the `schema.User` model. Permissions are ephemeral, provided exclusively by the Authenticator on each request, and never stored by ravnar. A new factory dependency `authorized_user_with(*permissions)` gates each endpoint, checking that the authenticated user possesses all required permissions. Existing item-level authorization (user-scoped data isolation) is untouched.

## Goals

- Gate every API endpoint behind task-level authorization using a permission system.
- Keep permissions ephemeral — sourced from the Authenticator, never persisted by ravnar.
- Make authz transparent to endpoint logic via a `Depends()` factory.
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

A permission is an opaque string with a validated format: `<resource>:<action>`.

- **Resources**: `files`, `threads`, `agents`.
- **Actions**: `read`, `write`, `delete`.

A `Permission` type enforces this format via a Pydantic `AfterValidator`. Invalid formats raise `ValueError`.

Permissions are stored as a field on `schema.User`:

```python
permissions: Annotated[list[Permission], Field(default_factory=list)] = Field(
    default_factory=list,
    # validator deduplicates and sorts alphabetically
)
```

A validator on the container deduplicates entries and sorts them alphabetically, ensuring canonical representation.

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

A new internal module at `src/_ravnar/auth.py` provides the authorization factory:

```python
def make_authorized_user_factory(
    security_config: SecurityConfig,
) -> tuple[Callable[..., Awaitable[schema.User]], Callable[..., Any]]:
    """Returns (authenticated_user, authorized_user_with)."""
```

The function:

1. **Creates `authenticated_user`** from `security_config.authenticator`, mirroring the current logic in `core.py`:
   - If no authenticator is configured, returns a default user (current system user).
   - If configured, instantiates the authenticator and resolves forward references on its `authenticate` method.

2. **Returns `authorized_user_with`** — a factory that takes `*permissions: str` and returns a FastAPI dependency:
   - The returned dependency accepts `user: schema.User = Depends(authenticated_user)` and checks that all required permissions are present in `user.permissions`.
   - If no permissions are passed (`authorized_user_with()`), it only authenticates the user without any permission gate.
   - Missing permissions raise `HTTPException(status_code=403, detail="Insufficient permissions")`.

The `authenticated_user` reference is captured in the closure of `authorized_user_with`, so FastAPI deduplicates the auth call across multiple `Depends()` invocations in the same request.

This module is only imported from `core.py`. The `authorized_user_with` factory is then passed to all router factories in place of `authenticated_user`.

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

### Router-Level Dependencies

The top-level API router (`src/_ravnar/api/__init__.py`) retains a router-level dependency for defense in depth:

```python
router = schema.APIRouter(tags=["API"], dependencies=[Depends(authenticated_user)])
```

This ensures every request to `/api/*` passes through authentication, even if an endpoint accidentally omits its `Depends()` declaration. The top-level dependency uses `authenticated_user` (not `authorized_user_with`) because it has no permission context.

Router-level dependencies are **removed** from all sub-routers (`threads.py`, `files.py`, `agents.py`). Per-endpoint `Depends(authorized_user_with(...))` declarations are the sole source of permission checks, making the required permissions visible in each endpoint's signature.

### `core.py` Changes

`core.py` becomes the single caller of `make_authorized_user_factory`:

```python
authenticated_user, authorized_user_with = make_authorized_user_factory(config.security)
```

The `authorized_user_with` factory is passed to `make_api_router()` and through to sub-routers.

### Schema Changes

#### `schema.User`

Add a `permissions` field:

```python
class User(BaseModel):
    id: str
    data: dict[str, Any] = Field(default_factory=dict)
    permissions: list[Permission] = Field(default_factory=list)
```

#### `Permission` Type

```python
Permission = Annotated[
    str,
    AfterValidator(_validate_permission_format),
]
```

The validator enforces `<resource>:<action>` format where both parts are non-empty, lowercase, alphanumeric (with underscores/hyphens allowed).

### Authenticator Changes

#### `DebugAuthenticator`

Returns a user with **all permissions** (acts as an admin):

```python
return schema.User(
    id="debug",
    permissions=["agents:delete", "agents:read", "agents:write",
                 "files:delete", "files:read", "files:write",
                 "threads:delete", "threads:read", "threads:write"],
    data={...},
)
```

Permissions are hardcoded as the full taxonomy, keeping them in the schema module as a canonical reference.

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

`BearerTokenAuthenticator` passes the parameter through from its `TokenValidator`.

### Config Changes

No changes to `SecurityConfig`. Authenticators are configured via `ImportStringWithParams`, so new parameters (`permissions_header`, `permissions_claim`) are provided through config YAML or environment variables.

### `GET /api/user` Response

The endpoint returns `schema.User` as-is, which now includes the `permissions` list. Clients can inspect what they are allowed to do.

## Tradeoffs & Risks

| Tradeoff | Explanation | Mitigation |
|---|---|---|
| Per-endpoint declarations are mandatory | Removing router-level dependencies from sub-routers means an endpoint without `Depends(authorized_user_with(...))` is unprotected (except by the top-level auth guard). | Top-level auth still blocks unauthenticated access. Code review and tests catch missing declarations. |
| Flat permissions, no roles | The Authenticator must map roles to permissions externally. Ravnar has no concept of "admin" or "editor" roles. | Acceptable — role semantics belong to the identity provider, not ravnar. |
| Permission typos silently fail | If the authenticator returns `"file:write"` instead of `"files:write"`, access is denied with no obvious error. | The `Permission` type validates format, and dedup/sort normalization catches duplicates. |
| `/api/config` remains authenticated | The top-level router dependency means even config requires authentication. | This is intentional — the current code already does this. |

## Testing Strategy

### Unit Tests

- **`Permission` validator**: Valid formats accepted, invalid formats rejected, container dedup/sort verified.
- **`authorized_user_with` factory**: 
  - User with all required permissions → returns user.
  - User missing one permission → 403.
  - User with no permissions calling `authorized_user_with()` (no args) → returns user.
  - Empty permission check with non-empty user permissions → returns user.
- **Authenticator changes**:
  - `DebugAuthenticator` returns full permission set.
  - `ForwardedUserAuthenticator` parses permissions from header, handles missing header.
  - `OIDCTokenValidator` with `permissions_claim`: extracts claim when present, raises 401 when claim is configured but missing from JWT, returns empty when not configured.

### Integration Tests

- **Permission-gated endpoints**: Test each endpoint with a user that has the correct permission (200) and without it (403).
- **Auth-only endpoints**: `GET /api/user` and `GET /api/config` work with any authenticated user, even with no permissions.
- **Top-level defense**: Verify that removing `Depends()` from an endpoint still blocks unauthenticated requests.
- **File hydration in runs**: Verify that `POST /api/threads/{id}/run` requires `files:write` (for file hydration).

### Test Fixtures

Extend `make_app_client` or the test conftest to support injecting users with specific permission sets. The `DebugAuthenticator` is already used in tests and will return all permissions, so existing tests should continue to pass. For negative tests, a custom authenticator or mock is needed.

## Open Questions

_No unresolved questions._
