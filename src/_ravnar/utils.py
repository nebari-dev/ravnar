from __future__ import annotations

import contextlib
import functools
import inspect
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from typing import Any, TypeVar, cast, get_type_hints

import structlog
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool
from typing_extensions import ParamSpec

T = TypeVar("T")
P = ParamSpec("P")

logger = structlog.get_logger()


def as_awaitable(fn: Callable[P, T] | Callable[P, Awaitable[T]], *args: P.args, **kwargs: P.kwargs) -> Awaitable[T]:
    if inspect.iscoroutinefunction(fn):
        fn = cast(Callable[..., Awaitable[T]], fn)
        awaitable = fn(*args, **kwargs)
    else:
        fn = cast(Callable[..., T], fn)
        awaitable = run_in_threadpool(fn, *args, **kwargs)

    return awaitable


def as_async_iterator(
    fn: Callable[..., Iterator[T]] | Callable[..., AsyncIterator[T]], *args: Any, **kwargs: Any
) -> AsyncIterator[T]:
    if inspect.isasyncgenfunction(fn):
        fn = cast(Callable[..., AsyncIterator[T]], fn)
        async_iterator = fn(*args, **kwargs)
    else:
        fn = cast(Callable[..., Iterator[T]], fn)
        async_iterator = iterate_in_threadpool(fn(*args, **kwargs))

    return async_iterator


class _AsyncContextManagerWrapper(contextlib.AbstractAsyncContextManager[T]):
    def __init__(self, cm: contextlib.AbstractContextManager[T]) -> None:
        self._cm = cm

    async def __aenter__(self) -> T:
        return await run_in_threadpool(self._cm.__enter__)

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: Any
    ) -> bool | None:
        return await run_in_threadpool(self._cm.__exit__, exc_type, exc_val, exc_tb)


def as_async_context_manager(
    cm: contextlib.AbstractContextManager[T] | contextlib.AbstractAsyncContextManager[T],
) -> contextlib.AbstractAsyncContextManager[T]:
    if isinstance(cm, contextlib.AbstractAsyncContextManager):
        return cm
    return _AsyncContextManagerWrapper(cm)


def kebabize(s: str) -> str:
    return re.sub(r"(([a-z])(?=[A-Z])|([A-Z])(?=[A-Z][a-z]))", r"\1-", s).lower()


def resolve_forward_references(c: Callable[..., T] | Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    annotations = get_type_hints(c, include_extras=True)
    signature = (s := inspect.signature(c)).replace(
        parameters=[
            p.replace(annotation=annotations[p.name] if p.annotation is not inspect.Parameter.empty else p.annotation)
            for p in s.parameters.values()
        ],
        return_annotation=annotations["return"]
        if s.return_annotation is not inspect.Signature.empty
        else s.return_annotation,
    )

    # This wrapper is required, because we cannot update the signature on some callable types directly. The wrapper must
    # handle the sync / async nature of the wrapped function manually, because the metadata is lost, which FastAPI
    # requires to do so automatically.
    @functools.wraps(c)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        return await as_awaitable(c, *args, **kwargs)

    wrapper.__annotations__ = annotations
    wrapper.__signature__ = signature  # type: ignore[attr-defined]

    return wrapper


def now() -> datetime:
    return datetime.now(tz=UTC)
