from __future__ import annotations

import base64
import dataclasses
import mimetypes
import urllib.parse
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Self

import ag_ui.core
import httpx
import pydantic
from fastapi import HTTPException, status
from opentelemetry import trace
from upath import UPath

from _ravnar import orm, schema
from _ravnar.config import FileStorageConfig, normalize_hostname
from _ravnar.observability import traced
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


FileInputContent = Annotated[
    ag_ui.core.ImageInputContent
    | ag_ui.core.AudioInputContent
    | ag_ui.core.VideoInputContent
    | ag_ui.core.DocumentInputContent,
    pydantic.Field(discriminator="type"),
]

MIME_TYPE = "application/vnd.ravnar.json-b64"


class DataSourceValue(schema.BaseModel):
    file_id: uuid.UUID
    mime_type: str
    source_type: str
    source_data: dict[str, Any] | None
    created_at: datetime

    @classmethod
    def decode(cls, s: str) -> Self:
        return cls.model_validate_json(base64.b64decode(s))

    def encode(self) -> str:
        return base64.b64encode(self.model_dump_json(by_alias=True).encode()).decode()


def convert_file_to_input_content(file: orm.File) -> FileInputContent:
    return pydantic.TypeAdapter(ag_ui.core.InputContent).validate_python(
        {
            "type": file.type,
            "source": ag_ui.core.InputContentDataSource(
                value=DataSourceValue(
                    file_id=file.id,
                    mime_type=file.mime_type,
                    source_type=file.source_type,
                    source_data=file.source_data,
                    created_at=file.created_at,
                ).encode(),
                mime_type=MIME_TYPE,
            ),
            "metadata": file.metadata_,
        }
    )


class WrappedMetadata(schema.BaseModel):
    raw: Any
    file_id: uuid.UUID


class FileHandler:
    def __init__(self, *, file_storage_config: "FileStorageConfig", database: Database) -> None:
        self._file_storage_config = file_storage_config
        self._storage = _Storage(file_storage_config.path)
        self._database = database

        self._extractors = {
            "data": self._extract_data,
            "url": self._extract_url,
            "custom": self._extract_custom,
        }

    def _validate_url(self, url: str) -> str:
        """Validate a URL against the SSRF guard config.

        Returns the validated URL string on success.
        Raises HTTPException(400) on failure.
        """
        config = self._file_storage_config.url_data_source

        if not config.enabled:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="URL file source is not enabled")

        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname

        if not hostname:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="URL fetch not allowed")

        try:
            normalized = normalize_hostname(hostname)
        except Exception:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="URL fetch not allowed")

        span = trace.get_current_span()
        span.set_attribute("ssrf.hostname", normalized)

        if not config.allowlist:
            span.set_attribute("ssrf.blocked_reason", "not_allowed")
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="URL fetch not allowed")

        # Wildcard sentinel — same pattern as Starlette CORSMiddleware
        if "*" in config.allowlist:
            return url

        allowed = False
        for entry in config.allowlist:
            if normalized == entry or normalized.endswith("." + entry):
                span.set_attribute("ssrf.allowlist_entry", entry)
                allowed = True
                break

        if not allowed:
            span.set_attribute("ssrf.blocked_reason", "not_allowed")
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="URL fetch not allowed")

        return url

    @traced
    async def add(self, file_input_content: FileInputContent, *, user_id: str) -> tuple[orm.File, bytes]:
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

        return file, data.content

    @traced
    async def add_or_read(self, file_input_content: FileInputContent, *, user_id: str) -> tuple[orm.File, bytes]:
        if (
            isinstance(file_input_content.source, ag_ui.core.InputContentDataSource)
            and file_input_content.source.mime_type == MIME_TYPE
        ):
            value = DataSourceValue.decode(file_input_content.source.value)
            file = await self.get(value.file_id, user_id=user_id)
            content = await self._storage.read(file.id)
        else:
            file, content = await self.add(file_input_content, user_id=user_id)

        return file, content

    @staticmethod
    async def _extract_data(file_input_content: FileInputContent) -> _FileData:
        assert isinstance(file_input_content.source, ag_ui.core.InputContentDataSource)

        return _FileData(
            content=await as_awaitable(base64.b64decode, file_input_content.source.value),
            mime_type=file_input_content.source.mime_type,
        )

    async def _extract_url(self, file_input_content: FileInputContent) -> _FileData:
        assert isinstance(file_input_content.source, ag_ui.core.InputContentUrlSource)

        url = file_input_content.source.value
        mime_type = file_input_content.source.mime_type
        max_redirects = 20
        timeout = self._file_storage_config.url_data_source.timeout
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("FileHandler.fetch_url"):
            self._validate_url(url)

            config = httpx.Timeout(timeout.total_seconds())
            async with httpx.AsyncClient(follow_redirects=False, timeout=config) as client:
                redirect_chain: list[str] = []
                current_url = url
                for _ in range(max_redirects):
                    response = await client.get(current_url)

                    if response.is_redirect:
                        location = response.headers.get("Location")
                        if not location:
                            span = trace.get_current_span()
                            exc = HTTPException(
                                status_code=status.HTTP_502_BAD_GATEWAY,
                                detail="Failed to fetch file from URL",
                            )
                            span.record_exception(exc)
                            span.set_status(trace.StatusCode.ERROR, description="Redirect missing Location header")
                            raise exc
                        redirect_chain.append(location)
                        self._validate_url(location)
                        current_url = location
                        continue

                    if not response.is_success:
                        span = trace.get_current_span()
                        exc = HTTPException(
                            status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to fetch file from URL"
                        )
                        span.record_exception(exc)
                        span.set_status(trace.StatusCode.ERROR, description="Failed to fetch file from URL")
                        raise exc

                    content = response.content
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    break
                else:
                    span = trace.get_current_span()
                    exc = HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to fetch file from URL"
                    )
                    span.record_exception(exc)
                    span.set_status(trace.StatusCode.ERROR, description="Too many redirects")
                    raise exc

        span = trace.get_current_span()
        if redirect_chain:
            span.set_attribute("ssrf.redirect_chain", redirect_chain)
            span.set_attribute("ssrf.redirect_count", len(redirect_chain))

        if not mime_type:
            mime_type = content_type
        if not mime_type:
            mime_type, _ = mimetypes.guess_type(current_url, strict=False)
        if not mime_type:
            mime_type = "application/octet-stream"

        return _FileData(content=content, mime_type=mime_type, source_data={"url": current_url})

    @staticmethod
    async def _extract_custom(file_input_content: FileInputContent) -> _FileData:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Custom file source type is not supported"
        )

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
