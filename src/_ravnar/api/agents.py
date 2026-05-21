from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any

import ag_ui.core
import fastsse
from fastapi import Depends, Path

from _ravnar import schema

if TYPE_CHECKING:
    from _ravnar.core import AgentHandler


def make_router(*, agent_handler: AgentHandler, authenticated_user: Callable[..., Any]) -> schema.APIRouter:
    router = schema.APIRouter(tags=["Agents"], dependencies=[Depends(authenticated_user)])

    @router.get("")
    async def list_agents() -> list[schema.AgentInfo]:
        return agent_handler.infos()

    @router.sse("/{agentId}/run", methods=["POST"], response_model=schema.Event, tags=["Runs"])
    async def create_stateless_run(
        *, agent_id: Annotated[str, Path(alias="agentId")], run_agent_input: ag_ui.core.RunAgentInput
    ) -> fastsse.Response:
        return await agent_handler.run(agent_id, run_agent_input)

    if agent_handler.dynamic_enabled:
        _make_dynamic_agents_router(router, agent_handler=agent_handler, authenticated_user=authenticated_user)

    return router


def _make_dynamic_agents_router(
    router: schema.APIRouter,
    *,
    agent_handler: AgentHandler,
    authenticated_user: Callable[..., Any],
) -> None:
    description = (
        "Only available if dynamic agents are enabled. "
        "Can be checked with [`GET /api/config`](#/API/get_config_api_config_get)."
    )

    @router.post("", description=description)
    async def register_agent(
        data: schema.RegisterAgentData,
    ) -> schema.AgentInfo:
        agent = data.agent()
        agent_handler.add_agent(data.id, agent)
        return schema.AgentInfo(
            id=data.id,
            capabilities=agent.get_capabilities(),
            quick_prompts=agent.get_quick_prompts(),
        )

    @router.delete("/{agentId}", description=description)
    async def unregister_agent(
        agent_id: Annotated[str, Path(alias="agentId")],
    ) -> None:
        agent_handler.remove_agent(agent_id)
