# AGENTS.md

## Project Overview

ravnar is a fully-fledged, pluggable [AG-UI](https://ag-ui.com) server written in Python. It is currently in **alpha**
state — code quality varies across modules.

### Tech Stack

- **Language**: Python ≥3.11
- **Package manager**: `uv`
- **Build system**: `hatchling` with dynamic versioning from git
- **Web framework**: FastAPI
- **ORM**: SQLAlchemy (async)
- **Settings**: pydantic-settings (YAML config)
- **Logging**: structlog
- **Observability**: OpenTelemetry

## Project Structure

- `src/ravnar/` — CLI entry points and public re-exports from `_ravnar`
- `src/_ravnar/` — Internal implementation (core server, API routes, ORM, schema)
- `tests/` — Test suite (pytest)
- `docs/` — MkDocs documentation + tutorials
- `scripts/` — Utility scripts
- `helm/` — Helm chart for deployment

## Commands

| Task         | Command                     |
| ------------ | --------------------------- |
| Install deps | `uv sync --group dev`       |
| Run tests    | `uv run pytest`             |
| Lint (check) | `uv run ruff check .`       |
| Lint (fix)   | `uv run ruff check --fix .` |
| Format       | `uv run ruff format .`      |
| Type check   | `uv run mypy`               |
| Run server   | `uv run ravnar serve`       |

## Code Style & Conventions

- **Line length**: 120
- **Docstrings**: Google style
- **Type hints**: Required on all function signatures. mypy is strict (disallows untyped defs, incomplete defs, and
  untyped calls)
- **Imports**: Sorted via ruff (`isort` convention) — stdlib, third-party, first-party
- **Formatter**: `ruff format`
- **Logging**: Use `structlog` — never use bare `print()`

## Testing Conventions

- Always add or update tests when modifying code
- Tests are in `tests/`
- Async tests must be defined as `async def test_*` — `pytest-asyncio` in auto mode handles the event loop
  automatically, no manual decorators needed
- Run tests with `uv run pytest`

## Documentation

- Any change to the configuration schema (`src/_ravnar/config.py`) must include a corresponding update to the
  configuration reference documentation (`docs/references/config.md`).

## Architecture & Patterns

- All implementation code goes in `src/_ravnar/`. The `src/ravnar/` package contains only the CLI and selective
  re-exports — the package is private by default
- The AG-UI protocol (`ag-ui-protocol`) is the core protocol. Respect its types and semantics when building agents,
  handling messages, or working with events
- **Database sessions are never public**: Public methods on the `Database` class manage their own sessions. Shared
  functionality lives in private methods (prefixed with `_`) that accept `session: AsyncSession` as the first parameter.
  Follow this pattern when adding new database operations
