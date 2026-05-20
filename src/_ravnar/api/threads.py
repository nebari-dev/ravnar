from __future__ import annotations

import base64
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any

import ag_ui.core
import fastsse
import pydantic
from fastapi import Depends, HTTPException, Path, Query, status
from opentelemetry import trace

from _ravnar import schema
from _ravnar.file_storage import FileHandler, WrappedMetadata
from _ravnar.observability import traced
from _ravnar.utils import as_awaitable

tracer = trace.get_tracer(__name__)

if TYPE_CHECKING:
    from _ravnar.database import Database
    from _ravnar.events import EventProcessor

    from . import AgentHandler

    ThreadsSortBy = str
    RunsSortBy = str
else:
    ThreadsSortBy = schema.create_str_literal("created_at", default="created_at")
    RunsSortBy = schema.create_str_literal("created_at", default="created_at")


def make_router(
    *,
    database: Database,
    file_handler: FileHandler,
    agent_handler: AgentHandler,
    authenticated_user: Callable[..., Any],
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
        thread_id: Annotated[str, Path(alias="threadId")],
        user: schema.User = Depends(authenticated_user),  # noqa: B008
    ) -> list[schema.AugmentedMessage]:
        _, _, messages = await database.get_thread_history(user_id=user.id, thread_id=thread_id, run_id=None)
        return pydantic.TypeAdapter(list[schema.AugmentedMessage]).validate_python(messages, from_attributes=True)

    @router.get("/{threadId}/runs")
    async def get_runs(
        *,
        user: schema.User = Depends(authenticated_user),  # noqa: B008
        thread_id: Annotated[str, Path(alias="threadId")],
        pagination: Annotated[schema.Pagination[RunsSortBy], Query()],
    ) -> schema.Page[schema.Run]:
        return schema.Page[schema.Run].model_validate(
            await database.get_runs(user_id=user.id, thread_id=thread_id, pagination=pagination),
            from_attributes=True,
        )

    @router.get("/{threadId}/runs/{runId}")
    async def get_run(
        *,
        user: schema.User = Depends(authenticated_user),  # noqa: B008
        thread_id: Annotated[str, Path(alias="threadId")],
        run_id: Annotated[str, Path(alias="runId")],
    ) -> schema.Run:
        return schema.Run.model_validate(await database.get_run(id=run_id, user_id=user.id), from_attributes=True)

    @router.get("/{threadId}/runs/{runId}/messages")
    async def get_run_messages(
        *,
        user: schema.User = Depends(authenticated_user),  # noqa: B008
        thread_id: Annotated[str, Path(alias="threadId")],
        run_id: Annotated[str, Path(alias="runId")],
    ) -> list[schema.AugmentedMessage]:
        _, _, messages = await database.get_thread_history(user_id=user.id, thread_id=thread_id, run_id=run_id)
        return pydantic.TypeAdapter(list[schema.AugmentedMessage]).validate_python(messages, from_attributes=True)

    @router.sse("/{threadId}/runs", methods=["POST"], response_model=schema.Event, tags=["Runs"])
    async def create_run(
        *,
        user: schema.User = Depends(authenticated_user),  # noqa: B008
        thread_id: Annotated[str, Path(alias="threadId")],
        data: schema.CreateRunData,
    ) -> fastsse.Response:
        thread, parent_run, parent_messages = await database.get_thread_history(
            user_id=user.id, thread_id=thread_id, run_id=data.parent_run_id
        )

        messages = pydantic.TypeAdapter(list[schema.AugmentedMessage]).validate_python(
            parent_messages, from_attributes=True
        )
        messages.extend(data.messages)

        await hydrate_files(messages, user=user, file_handler=file_handler)

        run_agent_input = ag_ui.core.RunAgentInput(
            thread_id=thread_id,
            run_id=data.id,
            parent_run_id=data.parent_run_id,
            state=parent_run.state if parent_run is not None else None,
            messages=[pydantic.TypeAdapter(ag_ui.core.Message).validate_python(m.model_dump()) for m in messages],
            tools=data.tools,
            context=data.context,
            forwarded_props=data.forwarded_props,
        )

        async def callback(event_processor: EventProcessor) -> None:
            run = event_processor.extract(include_input_message_ids={m.id for m in data.messages})
            await database.create_run(run)

        return await agent_handler.run(thread.agent_id, run_agent_input, callback=callback)

    @traced(name="file-hydration")
    async def hydrate_files(
        messages: list[schema.AugmentedMessage],
        *,
        user: schema.User,
        file_handler: FileHandler,
    ) -> None:
        for m in messages:
            if not isinstance(m, schema.AugmentedUserMessage):
                continue

            for input_content in m.content:
                if isinstance(input_content, ag_ui.core.TextInputContent):
                    continue
                if isinstance(input_content, ag_ui.core.BinaryInputContent):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="Binary input content is not supported",
                    )

                file, content = await file_handler.add_or_read(input_content, user_id=user.id)
                input_content.source = ag_ui.core.InputContentDataSource(
                    value=await as_awaitable(lambda c: base64.b64encode(c).decode(), content),
                    mime_type=file.mime_type,
                )
                input_content.metadata = WrappedMetadata(raw=input_content.metadata, file_id=file.id)

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
