from __future__ import annotations

import uuid

from upath import UPath

from _ravnar import schema
from .database import Database
from .utils import as_awaitable
import ag_ui.core

ag_ui.core.UserMessage

class FileStorage:
    def __init__(self, root: UPath) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, id: uuid.UUID) -> UPath:
        return self._root / str(id)

    def write(self, id: uuid.UUID, data: bytes) -> None:
        self._path(id).write_bytes(data)

    def read(self, id: uuid.UUID) -> bytes:
        return self._path(id).read_bytes()

    def delete(self, id: uuid.UUID) -> None:
        self._path(id).unlink()


class FileHandler:
    def __init__(self, *, database: Database, file_storage_path: UPath) -> None:
        self._database = database
        self._file_storage = FileStorage(file_storage_path)

    async def set(self, *, user: schema.User, file: schema.File, content: bytes | None) -> schema.File:
        if content:
            await as_awaitable(self._file_storage.write, file.id, content)
        elif file.external_url is None:
            raise ValueError("file must include content or an external_url")

        async with self._database.get_session() as session:
            await self._database.add_file(session, user_name=user.name, file=file)

        return file

    async def get_all(self, *, user: schema.User) -> list[schema.File]:
        async with self._database.get_session() as session:
            return await self._database.get_files(session, user_name=user.name)

    async def get(self, *, user: schema.User, id: uuid.UUID) -> schema.File | None:
        async with self._database.get_session() as session:
            return await self._database.get_file(session, user_name=user.name, id=id)

    async def delete(self, *, user: schema.User, id: uuid.UUID) -> None:
        file = await self.get(user=user, id=id)
        if file is None:
            return

        if file.external_url is not None:
            await as_awaitable(self._file_storage.delete, id)

        async with self._database.get_session() as session:
            await self._database.delete_file(session, user_name=user.name, id=id)

    async def read(self, *, file: schema.File) -> bytes:
        return await (
            as_awaitable(self._read_external_url, file.external_url)
            if file.external_url
            else as_awaitable(self._file_storage.read, file.id)
        )

    @staticmethod
    def _read_external_url(path: UPath) -> bytes:
        with path.open("rb") as f:
            return f.read()