from __future__ import annotations

import base64
import dataclasses
import mimetypes
import urllib.parse
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any, cast

import ag_ui.core
import httpx
import pydantic
from fastapi import HTTPException, status
from opentelemetry import trace
from upath import UPath

from _ravnar import orm, schema
from _ravnar.observability import traced
from _ravnar.utils import as_awaitable, normalize_hostname

if TYPE_CHECKING:
    from _ravnar.config import FileStorageConfig
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


FilePart = Annotated[
    ag_ui.core.ImagePart | ag_ui.core.AudioPart | ag_ui.core.VideoPart | ag_ui.core.DocumentPart,
    pydantic.Field(discriminator="type"),
]

RAVNAR_PROVIDER = "ravnar"


def convert_file_to_part(file: orm.File) -> FilePart:
    part_cls = {
        "image": ag_ui.core.ImagePart,
        "audio": ag_ui.core.AudioPart,
        "video": ag_ui.core.VideoPart,
        "document": ag_ui.core.DocumentPart,
    }[file.type]
    return cast(
        "FilePart",
        part_cls(
            source=ag_ui.core.FileSource(value=str(file.id), provider=RAVNAR_PROVIDER, mime_type=file.mime_type),
            metadata=file.metadata_,
        ),
    )


class WrappedMetadata(schema.BaseModel):
    raw: Any
    file_id: uuid.UUID


class FileHandler:
    def __init__(self, *, config: FileStorageConfig, database: Database) -> None:
        self._config = config
        self._storage = _Storage(config.path)
        self._database = database

    @traced
    async def add(self, file_part: FilePart, *, user_id: str) -> tuple[orm.File, bytes]:
        source_type = file_part.source.type
        try:
            extractor = {
                "data": self._extract_data,
                "url": self._extract_url,
            }[source_type]
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unsupported file source type"
            ) from None

        data = await extractor(file_part)
        file = orm.File(
            user_id=user_id,
            type=file_part.type,
            mime_type=data.mime_type,
            metadata_=file_part.metadata,
            source_type=source_type,
            source_data=data.source_data,
        )

        await self._storage.write(file.id, data.content)
        await self._database.add_file(file)

        return file, data.content

    @traced
    async def add_or_read(self, file_part: FilePart, *, user_id: str) -> tuple[orm.File, bytes]:
        if isinstance(file_part.source, ag_ui.core.FileSource) and file_part.source.provider == RAVNAR_PROVIDER:
            file = await self.get(uuid.UUID(file_part.source.value), user_id=user_id)
            content = await self._storage.read(file.id)
        else:
            file, content = await self.add(file_part, user_id=user_id)

        return file, content

    @staticmethod
    async def _extract_data(file_part: FilePart) -> _FileData:
        assert isinstance(file_part.source, ag_ui.core.DataSource)

        return _FileData(
            content=await as_awaitable(base64.b64decode, file_part.source.value),
            mime_type=file_part.source.mime_type,
        )

    async def _extract_url(self, file_part: FilePart) -> _FileData:
        assert isinstance(file_part.source, ag_ui.core.UrlSource)

        if not self._config.url_data_source.enabled:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="URL file source is not enabled")

        mime_type = file_part.source.mime_type

        response = await self._fetch_url(
            file_part.source.value,
            timeout=self._config.url_data_source.timeout,
            allowed_hostnames=self._config.url_data_source.allowed_hostnames,
        )

        url = str(response.request.url)
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
    @traced(name="FileHandler.fetch_url")
    async def _fetch_url(
        url: str,
        *,
        timeout: timedelta,  # noqa: ASYNC109
        allowed_hostnames: list[str],
        max_redirects: int = 20,
    ) -> httpx.Response:
        redirect_chain: list[str] = []
        failure_exception = HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to fetch file from URL"
        )
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout.total_seconds()) as client:
            for _ in range(max_redirects):
                response = await client.get(FileHandler._validate_url(url, allowed_hostnames=allowed_hostnames))
                next_request = response.next_request
                if next_request is not None:
                    url = str(next_request.url)
                    redirect_chain.append(url)
                    continue

                if not response.is_success:
                    raise failure_exception

                span = trace.get_current_span()
                span.set_attribute("ssrf.redirect_chain", redirect_chain)
                span.set_attribute("ssrf.redirect_count", len(redirect_chain))

                return response

            raise failure_exception

    @staticmethod
    def _validate_url(url: str, *, allowed_hostnames: list[str]) -> str:
        failure_exception = HTTPException(status.HTTP_400_BAD_REQUEST, detail="URL fetch not allowed")

        parts = urllib.parse.urlsplit(url)
        if not parts.hostname:
            raise failure_exception

        try:
            normalized_hostname = normalize_hostname(parts.hostname)
        except Exception as exc:
            raise failure_exception from exc

        if "*" in allowed_hostnames:
            return url

        for entry in allowed_hostnames:
            if normalized_hostname == entry or normalized_hostname.endswith("." + entry):
                return url

        raise failure_exception

    @traced
    async def get(self, id: uuid.UUID, *, user_id: str) -> orm.File:
        return await self._database.get_file(id=id, user_id=user_id)

    @traced
    async def read(self, id: uuid.UUID, *, user_id: str) -> tuple[str, bytes]:
        file = await self._database.get_file(id=id, user_id=user_id)
        content = await self._storage.read(file.id)
        return file.mime_type, content

    @traced
    async def delete(self, id: uuid.UUID, *, user_id: str) -> None:
        await self._database.delete_file(id=id, user_id=user_id)
        await self._storage.delete(id)
