from __future__ import annotations

import base64
import dataclasses
import mimetypes
import uuid
from typing import TYPE_CHECKING, Any

import ag_ui.core
import httpx
import pydantic
from fastapi import HTTPException, status
from upath import UPath

from _ravnar import ag_ui_input_content_compat, orm, schema
from _ravnar.utils import as_awaitable

if TYPE_CHECKING:
    from _ravnar.database import Database


class _Storage:
    def __init__(self, root: UPath) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, id: uuid.UUID) -> UPath:
        return self._root / str(id)

    async def write(self, id: uuid.UUID, content: bytes) -> None:
        await as_awaitable(self._path(id).write_bytes, content)

    async def read(self, id: uuid.UUID) -> bytes:
        return await as_awaitable(self._path(id).read_bytes)

    async def delete(self, id: uuid.UUID) -> None:
        return await as_awaitable(self._path(id).unlink)


@dataclasses.dataclass(kw_only=True)
class _FileData:
    content: bytes
    mime_type: str
    source_data: dict[str, Any] | None = None


class FileHandler:
    def __init__(self, *, root: UPath, database: Database) -> None:
        self._storage = _Storage(root)
        self._database = database

        self._extractors = {
            "data": self._extract_data,
            "url": self._extract_url,
            "custom": self._extract_custom,
        }

    @staticmethod
    def _file_to_input_content(file: orm.File) -> schema.RavnarFileInputContent:
        return pydantic.TypeAdapter(schema.RavnarFileInputContent).validate_python(
            {
                "type": file.type,
                "source": schema.InputContentRavnarSource(
                    value=schema.InputContentRavnarSourceValue(
                        file_id=file.id,
                        mime_type=file.mime_type,
                        source_type=file.source_type,
                        source_data=file.source_data,
                        created_at=file.created_at,
                    )
                ),
                "metadata": file.metadata_,
            }
        )

    async def add(
        self, file_input_content: schema.FileInputContent, *, user_id: str
    ) -> tuple[schema.RavnarFileInputContent, bytes]:
        source_type = file_input_content.source.type
        if source_type not in self._extractors:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unsupported file source type")

        data = await self._extractors[source_type](file_input_content)
        file = orm.File(
            user_id=user_id,
            type=file_input_content.type,
            mime_type=data.mime_type,
            metadata_=file_input_content.metadata,
            source_type=source_type,
            source_data=data.source_data,
        )

        await self._storage.write(file.id, data.content)
        await self._database.add_file(file)

        return self._file_to_input_content(file), data.content

    async def add_or_read(
        self, file_input_content: schema.FileInputContent, *, user_id: str
    ) -> tuple[schema.RavnarFileInputContent, bytes]:
        rfic: schema.RavnarFileInputContent
        if (
            isinstance(file_input_content.source, ag_ui_input_content_compat.InputContentCustomSource)
            and file_input_content.source.name == "ravnar"
        ):
            rfic = pydantic.TypeAdapter(schema.RavnarFileInputContent).validate_python(
                file_input_content, from_attributes=True
            )
            _, content = await self.read(file_input_content.source.value.file_id, user_id=user_id)
        else:
            rfic, content = await self.add(file_input_content, user_id=user_id)

        return rfic, content

    @staticmethod
    async def _extract_data(file_input_content: schema.FileInputContent) -> _FileData:
        assert isinstance(file_input_content.source, ag_ui.core.InputContentDataSource)

        return _FileData(
            content=await as_awaitable(base64.b64decode, file_input_content.source.value),
            mime_type=file_input_content.source.mime_type,
        )

    @staticmethod
    async def _extract_url(file_input_content: schema.FileInputContent) -> _FileData:
        assert isinstance(file_input_content.source, ag_ui.core.InputContentUrlSource)

        url = file_input_content.source.value
        mime_type = file_input_content.source.mime_type
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url)
            if not response.is_success:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to fetch file from URL")
            content = response.content
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()

        if not mime_type:
            mime_type = content_type
        if not mime_type:
            mime_type, _ = mimetypes.guess_type(url, strict=False)
        if not mime_type:
            mime_type = "application/octet-stream"

        return _FileData(content=content, mime_type=mime_type, source_data={"url": url})

    @staticmethod
    async def _extract_custom(file_input_content: schema.FileInputContent) -> _FileData:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Custom file source type is not supported"
        )

    async def get(self, id: uuid.UUID, *, user_id: str) -> schema.RavnarFileInputContent:
        file = await self._database.get_file(id=id, user_id=user_id)
        return self._file_to_input_content(file)

    async def read(self, id: uuid.UUID, *, user_id: str) -> tuple[str, bytes]:
        file = await self._database.get_file(id=id, user_id=user_id)
        content = await self._storage.read(id)
        return file.mime_type, content

    async def delete(self, id: uuid.UUID, *, user_id: str) -> None:
        await self._database.delete_file(id=id, user_id=user_id)
        await self._storage.delete(id)
