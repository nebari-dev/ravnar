from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends

from _ravnar import schema
from _ravnar.auth import User

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
    authorized_user_with: Callable[..., Any],
) -> schema.APIRouter:
    router = schema.APIRouter(
        tags=["API"],
        # This ensures that every endpoint on this router or its sub-routers
        # can only be accessed by authenticated users. The actual authorization
        # check happens on the specific endpoint.
        dependencies=[Depends(authorized_user_with())],
    )

    @router.get("/user")
    async def get_user(
        user: User = Depends(authorized_user_with()),  # noqa: B008
    ) -> User:
        return user

    @router.get("/config")
    async def get_config() -> schema.APIConfig:
        return schema.APIConfig(dynamic_agents_enabled=agent_handler.dynamic_enabled)

    router.include_router(
        make_files_router(file_handler=file_handler, authorized_user_with=authorized_user_with),
        prefix="/files",
    )

    router.include_router(
        make_threads_router(
            database=database,
            file_handler=file_handler,
            agent_handler=agent_handler,
            authorized_user_with=authorized_user_with,
        ),
        prefix="/threads",
    )

    router.include_router(
        make_agents_router(agent_handler=agent_handler, authorized_user_with=authorized_user_with), prefix="/agents"
    )

    return router
