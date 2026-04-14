from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any

import ag_ui.core
import ag_ui.encoder
import fastsse
import pydantic
from fastapi import Depends, Path, Query

from _ravnar import schema
from _ravnar.utils import now

if TYPE_CHECKING:
    from _ravnar.database import Database
    from _ravnar.events import EventProcessor

    from . import AgentHandler

    ThreadsSortBy = str
else:
    ThreadsSortBy = schema.create_str_literal("created_at", "updated_at", default="created_at")


def make_router(
    *, database: Database, agent_handler: AgentHandler, authenticated_user: Callable[..., Any]
) -> schema.APIRouter:
    router = schema.APIRouter(tags=["Threads"], dependencies=[Depends(authenticated_user)])

    @router.post("")
    async def create_thread(
        *,
        user: schema.User = Depends(authenticated_user),  # noqa: B008
        data: schema.CreateThreadData,
    ) -> schema.Thread:
        agent_handler.assert_available(data.agent_id)
        return schema.Thread.model_validate(
            await database.create_thread(user_id=user.id, id=data.id, name=data.name, agent_id=data.agent_id),
            from_attributes=True,
        )

    @router.get("")
    async def get_threads(
        *,
        user: schema.User = Depends(authenticated_user),  # noqa: B008
        pagination: Annotated[schema.Pagination[ThreadsSortBy], Query()],
    ) -> schema.Page[schema.Thread]:
        return schema.Page[schema.Thread].model_validate(
            await database.get_threads(user_id=user.id, pagination=pagination), from_attributes=True
        )

    @router.get("/{threadId}")
    async def get_thread(
        id: Annotated[str, Path(alias="threadId")],
        user: schema.User = Depends(authenticated_user),  # noqa: B008
    ) -> schema.Thread:
        return schema.Thread.model_validate(await database.get_thread(user_id=user.id, id=id), from_attributes=True)

    @router.get("/{threadId}/messages")
    async def get_thread_messages(
        id: Annotated[str, Path(alias="threadId")],
        user: schema.User = Depends(authenticated_user),  # noqa: B008
    ) -> list[schema.AugmentedMessage]:
        thread = await database.get_thread(user_id=user.id, id=id, with_messages=True)
        return pydantic.TypeAdapter(list[schema.AugmentedMessage]).validate_python(
            thread.messages, from_attributes=True
        )

    @router.sse("/{threadId}/run", methods=["POST"], response_model=schema.Event, tags=["Runs"])
    async def create_run(
        *,
        user: schema.User = Depends(authenticated_user),  # noqa: B008
        thread_id: Annotated[str, Path(alias="threadId")],
        data: schema.CreateRunData,
    ) -> fastsse.Response:
        thread = await database.get_thread(user_id=user.id, id=thread_id, with_messages=True)

        messages = pydantic.TypeAdapter(list[schema.AugmentedMessage]).validate_python(
            thread.messages, from_attributes=True
        )
        messages.extend(data.messages)

        run_agent_input = ag_ui.core.RunAgentInput(
            thread_id=thread.id,
            run_id=str(uuid.uuid4()),
            parent_run_id=None,
            state=thread.state,
            messages=[pydantic.TypeAdapter(ag_ui.core.Message).validate_python(m.model_dump()) for m in messages],
            tools=data.tools,
            context=data.context,
            forwarded_props=data.forwarded_props,
        )

        async def callback(event_processor: EventProcessor) -> None:
            thread.state, thread.messages = event_processor.extract()
            thread.updated_at = now()
            await database.update_thread(thread)

        return await agent_handler.run(thread.agent_id, run_agent_input, callback=callback)

    @router.post("/{threadId}/rename")
    async def rename_thread(
        *,
        user: schema.User = Depends(authenticated_user),  # noqa: B008
        id: Annotated[str, Path(alias="threadId")],
        data: schema.RenameThreadData,
    ) -> schema.Thread:
        return schema.Thread.model_validate(
            await database.rename_thread(user_id=user.id, id=id, name=data.name), from_attributes=True
        )

    @router.delete("")
    async def delete_threads(
        *,
        user: schema.User = Depends(authenticated_user),  # noqa: B008
        data: schema.DeleteThreadsData,
    ) -> None:
        await database.delete_threads(user_id=user.id, ids=data.ids)

    @router.delete("/{threadId}")
    async def delete_thread(
        *,
        user: schema.User = Depends(authenticated_user),  # noqa: B008
        thread_id: Annotated[str, Path(alias="threadId")],
    ) -> None:
        await database.delete_threads(user_id=user.id, ids=[thread_id])

    return router
