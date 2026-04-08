from __future__ import annotations

import asyncio
import contextlib
import pickle
from collections.abc import AsyncIterator, Callable
from typing import Any, Generic, TypeVar, cast

from .utils import as_awaitable

T = TypeVar("T")


class SetupTeardownMixin:
    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    @staticmethod
    def lifespan_factory(*objs: SetupTeardownMixin) -> Callable[[Any], contextlib.AbstractAsyncContextManager[None]]:
        @contextlib.asynccontextmanager
        async def lifespan(_: Any) -> AsyncIterator[None]:
            await asyncio.gather(*[as_awaitable(obj.setup) for obj in objs])
            try:
                yield
            finally:
                await asyncio.gather(*[as_awaitable(obj.teardown) for obj in objs])

        return lifespan


class DeSerializeMixin(Generic[T]):
    @staticmethod
    def serialize(v: T) -> bytes:
        return pickle.dumps(v, protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def deserialize(data: bytes) -> T:
        return cast(T, pickle.loads(data))
