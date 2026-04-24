from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends

from _ravnar import schema

from .agents import make_router as make_agents_router
from .files import make_router as make_files_router
from .threads import make_router as make_threads_router

if TYPE_CHECKING:
    from _ravnar.core import AgentHandler
    from _ravnar.database import Database
    from _ravnar.file_storage import FileHandler


def make_router(
    *,
    database: Database,
    file_handler: FileHandler,
    agent_handler: AgentHandler,
    authenticated_user: Callable[..., Any],
) -> schema.APIRouter:
    router = schema.APIRouter(tags=["API"], dependencies=[Depends(authenticated_user)])

    @router.get("/user")
    async def get_user(
        user: schema.User = Depends(authenticated_user),  # noqa: B008
    ) -> schema.User:
        return user

    # FIXME: cache
    @router.get("/config")
    async def get_config() -> schema.APIConfig:
        return schema.APIConfig(agents=agent_handler.configs)

    router.include_router(
        make_files_router(file_handler=file_handler, authenticated_user=authenticated_user),
        prefix="/files",
    )

    router.include_router(
        make_threads_router(
            database=database,
            file_handler=file_handler,
            agent_handler=agent_handler,
            authenticated_user=authenticated_user,
        ),
        prefix="/threads",
    )

    router.include_router(
        make_agents_router(agent_handler=agent_handler, authenticated_user=authenticated_user), prefix="/agents"
    )

    return router
