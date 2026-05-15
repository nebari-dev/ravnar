# Design: Stateless Server Mode

## Summary

Add an opt-in stateless mode to ravnar that disables all persistence (database and file storage) while preserving observability (logging and tracing). In stateless mode the threads and files API routers are excluded, making `/api/agents/{agentId}/run` the only available agent interaction path. The mode is configured via `storage.enabled` and surfaced through the `/api/config` response.

## Goals

- Allow running ravnar as a fully stateless server with no database, no file storage, and no persistent state.
- Preserve full observability (structured logging, OTLP tracing) in stateless mode.
- Exclude the `/api/threads` and `/api/files` routers entirely when stateless so the OpenAPI schema accurately reflects available endpoints.
- Expose the storage mode through `/api/config` so API consumers can discover it.

## Non-Goals

- Partial statelessness (e.g., database without files, or files without database). A single flag controls both.
- Removing or altering the existing stateful code paths. Stateless mode is additive, not a rewrite.
- Changing how the existing `/api/agents/{agentId}/run` endpoint works — it is already stateless and requires no changes.

## Background / Motivation

ravnar is a batteries-included AG-UI server. In its default (stateful) mode it persists threads, runs, messages, and uploaded files to a database and local filesystem. This is useful for multi-turn conversations and long-running agent interactions.

However, some deployments only need single-shot agent invocations with no persistence. The `/api/agents/{agentId}/run` endpoint already supports this pattern — it accepts a full `RunAgentInput`, streams agent events back via SSE, and writes nothing to the database. But today the server still requires a database and file storage to start up, and the threads/files endpoints are unconditionally exposed, which is misleading to consumers in a stateless deployment.

A `storage.enabled` flag lets operators disable all storage infrastructure and have the API surface match the actual capabilities of the running server.

## Design

### Configuration

A new field `enabled: bool` is added to `StorageConfig` (in `src/_ravnar/config.py`), defaulting to `True`:

```python
class StorageConfig(BaseModel, RenderableMixin):
    enabled: bool = True
    database_dsn: str = Field(default_factory=lambda: f"sqlite:///{_local_storage() / 'state.db'}")
    file_storage_path: UPath = Field(default_factory=lambda: UPath(_local_storage() / "files"))
```

When `enabled=False`, the `database_dsn` and `file_storage_path` values are ignored. Their default factories may still run during model construction; this is an accepted minor cosmetic issue (the `.ravnar_local/` directory may be created unnecessarily) but has no functional impact. A follow-up improvement could use field validators to skip default evaluation when `enabled=False`.

Environment variable: `RAVNAR_STORAGE__ENABLED=false`

YAML:
```yaml
storage:
  enabled: false
```

### API Router Construction

`make_api_router` in `src/_ravnar/api/__init__.py` is refactored so that all stateful concerns (database construction, file handler, threads and files routers, database lifecycle) live in a dedicated `_make_stateful_router` function. `make_router` delegates to it when storage is enabled; otherwise it only includes the stateless-capable routers.

The agents sub-router (`/api/agents`) is **always included** regardless of the storage flag, since stateless agent runs do not require storage.

```python
def _make_stateful_router(
    *,
    storage_config: StorageConfig,
    agent_handler: AgentHandler,
    authenticated_user: Callable[..., Any],
) -> schema.APIRouter:
    database = Database(url=str(storage_config.database_dsn))
    file_handler = FileHandler(root=storage_config.file_storage_path, database=database)

    router = schema.APIRouter(tags=["Stateful"])
    router.add_event_handler("startup", database.setup)
    router.add_event_handler("shutdown", database.teardown)

    router.include_router(make_files_router(file_handler=file_handler, authenticated_user=authenticated_user), prefix="/files")
    router.include_router(make_threads_router(database=database, file_handler=file_handler, agent_handler=agent_handler, authenticated_user=authenticated_user), prefix="/threads")

    return router


def make_router(
    *,
    storage_config: StorageConfig,
    agent_handler: AgentHandler,
    authenticated_user: Callable[..., Any],
) -> schema.APIRouter:
    router = schema.APIRouter(tags=["API"], dependencies=[Depends(authenticated_user)])

    # ... /user and /config endpoints (always present) ...

    if storage_config.enabled:
        router.include_router(_make_stateful_router(
            storage_config=storage_config,
            agent_handler=agent_handler,
            authenticated_user=authenticated_user,
        ))

    router.include_router(make_agents_router(agent_handler=agent_handler, authenticated_user=authenticated_user), prefix="/agents")

    return router
```

### App Construction (`_make_app`)

`_make_app` in `src/_ravnar/core.py` is simplified: it no longer constructs `Database` or `FileHandler`, and no longer wires a custom lifespan. Instead it passes `config.storage` to `make_api_router`, which handles everything internally.

```python
def _make_app(self, config: BaseConfig) -> FastAPI:
    app = FastAPI(
        title="ravnar",
        version=__version__,
        root_path=config.server.root_path,
    )

    # ... CORS, authenticator, health/version/redirect routes ...

    agent_handler = AgentHandler(config.agents)

    api_router = make_api_router(
        storage_config=config.storage,
        agent_handler=agent_handler,
        authenticated_user=authenticated_user,
    )
    app.include_router(api_router, prefix="/api")

    return app
```

### `/api/config` Response

`APIConfig` in `src/_ravnar/schema/api.py` gains a `storage_enabled: bool` field:

```python
class APIConfig(BaseModel):
    agents: list[AgentConfig]
    storage_enabled: bool
```

The `/api/config` endpoint in `make_api_router` reads the flag and includes it:

```python
@router.get("/config")
async def get_config() -> schema.APIConfig:
    return schema.APIConfig(agents=agent_handler.configs, storage_enabled=storage_enabled)
```

This field is **always present** (`true` or `false`), allowing API consumers to discover the mode and decide which endpoints are safe to call.

### Excluded Endpoints in Stateless Mode

When `storage.enabled=False`, the following paths are **not registered** and will return FastAPI's standard 404 response:

| Method | Path |
|--------|------|
| `POST` | `/api/threads` |
| `GET`  | `/api/threads` |
| `GET`  | `/api/threads/{threadId}` |
| `GET`  | `/api/threads/{threadId}/messages` |
| `GET`  | `/api/threads/{threadId}/runs` |
| `GET`  | `/api/threads/{threadId}/runs/{runId}` |
| `GET`  | `/api/threads/{threadId}/runs/{runId}/messages` |
| `POST` | `/api/threads/{threadId}/runs` |
| `POST` | `/api/threads/{threadId}/rename` |
| `DELETE` | `/api/threads` |
| `DELETE` | `/api/threads/{threadId}` |
| `POST` | `/api/files` |
| `GET`  | `/api/files/{id}` |
| `GET`  | `/api/files/{id}/content` |
| `DELETE` | `/api/files/{id}` |

Endpoints that remain available in both modes:

| Method | Path | Notes |
|--------|------|-------|
| `GET`  | `/` | Redirect to docs |
| `GET`  | `/health` | Health check |
| `GET`  | `/version` | Server version |
| `GET`  | `/api/user` | Authenticated user info |
| `GET`  | `/api/config` | Agent configs + storage mode |
| `POST` | `/api/agents/{agentId}/run` | Stateless agent run (SSE) |

### Observability

Logging and tracing are configured at the top of `Ravnar.__init__` independently of storage. They are **not affected** by the `storage.enabled` flag. In stateless mode, structured logging and OTLP tracing operate identically to stateful mode.

## Tradeoffs & Risks

| Tradeoff / Risk | Mitigation |
|---|---|
| `database_dsn` and `file_storage_path` default factories still run in stateless mode, potentially creating `.ravnar_local/` directories | Accepted as cosmetic. Can be hardened later with a field validator that skips default evaluation when `enabled=False`. |
| `make_api_router` now owns storage construction and lifecycle | Previously `Database` was constructed in `_make_app` and passed in. Moving ownership into `make_api_router` keeps all storage concerns in one place. Database lifecycle is handled via router-level startup/shutdown events, eliminating the need for `_make_app` to wire the lifespan. |
| No independent toggles for database vs. file storage | Out of scope. The design assumes "stateless" is an all-or-nothing concept. If a use case for partial statelessness emerges, the flag can be split later. |
| Test coverage for stateless mode | Tests should be added: a stateless app client that verifies agents run works, threads/files return 404, and `/api/config` returns `storage_enabled: false`. |
