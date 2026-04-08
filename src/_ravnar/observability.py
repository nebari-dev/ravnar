from __future__ import annotations

import json
import types
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
import fastapi
import l2sl
import sqlalchemy
import starlette
import structlog
import uvicorn
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
    SpanProcessor,
)

from .version import __version__

if TYPE_CHECKING:
    from structlog.typing import EventDict, Processor, WrappedLogger

    from .config import BaseConfig


def _drop_health_probe_access_logs(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    if event_dict.get("logger") == "uvicorn.access" and event_dict["endpoint"] == "/health":
        raise structlog.DropEvent()

    return event_dict


def _drop_loggers(*loggers: str) -> Processor:
    def drop_logs(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        if event_dict.get("logger") in loggers:
            raise structlog.DropEvent()

        return event_dict

    return drop_logs


class LazyValue:
    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory

    def __call__(self) -> Any:
        return self._factory()

    @staticmethod
    def evaluate(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        return {k: v() if isinstance(v, LazyValue) else v for k, v in event_dict.items()}


def configure_logging(config: BaseConfig) -> None:
    suppress_locals: list[types.ModuleType | str] = [
        anyio,
        fastapi,
        sqlalchemy,
        starlette,
        uvicorn,
        # PEP 420 namespace packages need to be passed as string path
        *[
            str(
                next(
                    p
                    for p in Path(cast(str, package.__file__)).parents
                    if p.is_dir() and p.name == namespace_package_name
                )
            )
            for namespace_package_name, package in [("opentelemetry", trace)]
        ],
    ]

    structlog.configure(
        cache_logger_on_first_use=True,
        wrapper_class=structlog.make_filtering_bound_logger(config.server.logging.level.structlog_name),
        processors=[
            *(
                [
                    _drop_health_probe_access_logs,
                    _drop_loggers("httpx"),
                ]
                if config.server.logging.level > "debug"
                else [
                    LazyValue.evaluate,
                    structlog.processors.CallsiteParameterAdder(additional_ignores=["l2sl"]),
                ]
            ),
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.dev.set_exc_info,
            *(
                [  # type: ignore[list-item]
                    structlog.processors.ExceptionRenderer(
                        structlog.processors.ExceptionDictTransformer(suppress=suppress_locals)
                    ),
                    structlog.processors.JSONRenderer(),
                ]
                if config.server.logging.as_json
                else [
                    structlog.dev.ConsoleRenderer(
                        exception_formatter=structlog.dev.RichTracebackFormatter(suppress=suppress_locals)
                    ),
                ]
            ),
        ],
    )

    l2sl.configure_stdlib_log_forwarding()


def configure_tracing(config: BaseConfig) -> None:
    span_processors: list[SpanProcessor] = []
    if config.server.tracing.endpoint is not None:
        otlp_exporter = OTLPSpanExporter(endpoint=config.server.tracing.endpoint)
        span_processors.append(BatchSpanProcessor(otlp_exporter))
    if config.server.tracing.as_logs:
        structlog_exporter = StructlogSpanExporter()
        span_processors.append(SimpleSpanProcessor(structlog_exporter))

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


class StructlogSpanExporter(SpanExporter):
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
