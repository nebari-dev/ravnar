from __future__ import annotations

__all__ = ["StructlogSpanExporter", "configure", "traced"]

import contextlib
import functools
import inspect
import json
from collections.abc import Callable, Iterator, Sequence
from typing import TYPE_CHECKING, ParamSpec, TypeVar, overload

import structlog
from fastapi import HTTPException
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from _ravnar.version import __version__

if TYPE_CHECKING:
    from _ravnar.config import TracingConfig

P = ParamSpec("P")
T = TypeVar("T")


def configure(config: TracingConfig) -> None:
    span_processors = [factory() for factory in config.span_processors]

    if not span_processors:
        return

    resource = Resource.create().merge(
        Resource.create(
            {
                "service.name": "ravnar",
                "service.version": __version__,
            }
        )
    )
    tracer_provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(tracer_provider)

    for sp in span_processors:
        tracer_provider.add_span_processor(sp)


tracer = trace.get_tracer("ravnar.instrumentation")


def _traced(fn: Callable[P, T], *, name: str | None) -> Callable[P, T]:
    if name is None:
        name = f"{fn.__qualname__}"

    @contextlib.contextmanager
    def traced() -> Iterator[None]:
        with tracer.start_as_current_span(name):
            try:
                yield
            except HTTPException as exc:
                span = trace.get_current_span()
                span.add_event(
                    "http_exception",
                    attributes={"http.status_code": exc.status_code, "error.detail": exc.detail},
                )
                raise

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_fn_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            with traced():
                return await fn(*args, **kwargs)  # type: ignore[no-any-return]

        return async_fn_wrapper  # type: ignore[return-value]

    @functools.wraps(fn)
    def sync_fn_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        with traced():
            return fn(*args, **kwargs)

    return sync_fn_wrapper


@overload
def traced(fn: Callable[P, T], /) -> Callable[P, T]: ...


@overload
def traced(fn: Callable[P, T], /, *, name: str | None = None) -> Callable[P, T]: ...


@overload
def traced(*, name: str | None = None) -> Callable[[Callable[P, T]], Callable[P, T]]: ...


def traced(
    fn: Callable[P, T] | None = None, /, *, name: str | None = None
) -> Callable[P, T] | Callable[[Callable[P, T]], Callable[P, T]]:
    def decorator(fn: Callable[P, T]) -> Callable[P, T]:
        return _traced(fn, name=name)

    if fn is None:
        return decorator
    return decorator(fn)


class StructlogSpanExporter(SpanExporter):
    """structlog span exporter"""

    def __init__(self) -> None:
        self._logger = structlog.get_logger()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            for span in spans:
                self._logger.info("span", **json.loads(span.to_json(indent=None)))
            return SpanExportResult.SUCCESS
        except Exception:
            self._logger.exception("span export")
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
