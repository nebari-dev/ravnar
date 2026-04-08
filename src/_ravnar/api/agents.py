from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any

import ag_ui.core
import ag_ui.encoder
import fastsse
from fastapi import Depends, Path

from _ravnar import schema

if TYPE_CHECKING:
    from . import AgentHandler


def make_router(*, agent_handler: AgentHandler, authenticated_user: Callable[..., Any]) -> schema.APIRouter:
    router = schema.APIRouter(tags=["Agents"], dependencies=[Depends(authenticated_user)])

    @router.sse("/{agentId}/run", methods=["POST"], response_model=schema.Event, tags=["Runs"])
    async def create_stateless_run(
        *, agent_id: Annotated[str, Path(alias="agentId")], run_agent_input: ag_ui.core.RunAgentInput
    ) -> fastsse.Response:
        return await agent_handler.run(agent_id, run_agent_input)

    return router
