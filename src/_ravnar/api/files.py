from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Response

from _ravnar.file_storage import FileHandler, FileInputContent, convert_file_to_input_content
from _ravnar.security import User


def make_router(*, file_handler: FileHandler, authorized_user_with: Callable[..., Any]) -> APIRouter:
    router = APIRouter(tags=["Files"])

    @router.post("")
    async def upload_file(
        *,
        user: User = Depends(authorized_user_with("files:write")),  # noqa: B008
        file_input_content: Annotated[FileInputContent, Body()],
    ) -> FileInputContent:
        file, _ = await file_handler.add(file_input_content, user_id=user.id)
        return convert_file_to_input_content(file)

    @router.get("/{id}")
    async def get_file(
        *,
        user: User = Depends(authorized_user_with("files:read")),  # noqa: B008
        id: uuid.UUID,
    ) -> FileInputContent:
        return convert_file_to_input_content(await file_handler.get(id, user_id=user.id))

    @router.get("/{id}/content")
    async def read_file(
        *,
        user: User = Depends(authorized_user_with("files:read")),  # noqa: B008
        id: uuid.UUID,
    ) -> Response:
        mime_type, content = await file_handler.read(id, user_id=user.id)
        return Response(
            content,
            media_type=mime_type,
            headers={"Cache-Control": ", ".join(["private", "max-age=31536000", "immutable"])},
        )

    @router.delete("/{id}")
    async def delete_file(
        *,
        user: User = Depends(authorized_user_with("files:delete")),  # noqa: B008
        id: uuid.UUID,
    ) -> None:
        await file_handler.delete(id, user_id=user.id)

    return router
