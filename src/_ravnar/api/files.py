from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Response

from _ravnar import schema

if TYPE_CHECKING:
    from _ravnar.file_storage import FileHandler


def make_router(*, file_handler: FileHandler, authenticated_user: Callable[..., Any]) -> APIRouter:
    router = APIRouter(tags=["Files"])

    @router.post("")
    async def upload_file(
        *,
        user: schema.User = Depends(authenticated_user),  # noqa: B008
        file_input_content: schema.FileInputContent,
    ) -> schema.RavnarFileInputContent:
        file, _ = await file_handler.add(file_input_content, user_id=user.id)
        return file

    @router.get("/{id}")
    async def get_file(
        *,
        user: schema.User = Depends(authenticated_user),  # noqa: B008
        id: uuid.UUID,
    ) -> schema.RavnarFileInputContent:
        return await file_handler.get(id, user_id=user.id)

    @router.get("/{id}/content")
    async def read_file(
        *,
        user: schema.User = Depends(authenticated_user),  # noqa: B008
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
        user: schema.User = Depends(authenticated_user),  # noqa: B008
        id: uuid.UUID,
    ) -> None:
        await file_handler.delete(id, user_id=user.id)

    return router
