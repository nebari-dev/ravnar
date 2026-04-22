import asyncio
import uuid
from typing import Callable, Any

from fastapi import APIRouter, Response, UploadFile

from _ravnar import schema


def make_router(*, file_handler: FileHandler, authenticated_user: Callable[..., Any]) -> APIRouter:
    router = APIRouter(tags=["Files"])

    @router.post("/upload")
    async def upload_files(*, user: schema.User = authenticated_user, files: list[UploadFile]) -> list[schema.File]:
        async def upload(file: UploadFile) -> schema.File:
            return await file_handler.set(
                user=user,
                file=schema.File.from_params(
                    schema.FileParameters(
                        content_type=file.content_type or "application/octet-stream", size=file.size, name=file.filename
                    )
                ),
                content=await file.read(),
            )

        return await asyncio.gather(*[upload(f) for f in files])

    @router.post("/register")
    async def register_files(
        *, user: schema.User = authenticated_user, params: list[schema.FileParameters]
    ) -> list[schema.File]:
        return await asyncio.gather(
            *[file_handler.set(user=user, file=schema.File.from_params(p), content=None) for p in params]
        )

    @router.get("")
    async def get_files(*, user: schema.User = authenticated_user) -> list[schema.File]:
        return await file_handler.get_all(user=user)

    @router.get("/{id}")
    async def get_file(*, user: schema.User = authenticated_user, id: uuid.UUID) -> schema.File:
        file = await file_handler.get(user=user, id=id)
        if file is None:
            raise Exception
        return file

    @router.get("/{id}/content")
    async def read_file(*, user: schema.User = authenticated_user, id: uuid.UUID) -> Response:
        file = await get_file(user=user, id=id)
        return Response(
            await file_handler.read(file=file),
            media_type=file.content_type,
            headers={"Cache-Control": ", ".join(["private", "max-age=31536000", "immutable"])},
        )

    @router.delete("/{id}")
    async def delete_file(*, user: schema.User = authenticated_user, id: uuid.UUID) -> None:
        await file_handler.delete(user=user, id=id)

    return router