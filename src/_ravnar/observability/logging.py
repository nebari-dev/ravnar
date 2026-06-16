from __future__ import annotations

__all__ = ["LazyValue", "configure_logging"]

import types
from collections.abc import Callable
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

if TYPE_CHECKING:
    from structlog.typing import EventDict, Processor, WrappedLogger

    from _ravnar.config import LoggingConfig


def configure_logging(config: LoggingConfig) -> None:
    show_locals = config.level <= "debug"
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
        wrapper_class=structlog.make_filtering_bound_logger(config.level.structlog_name),
        processors=[
            *(
                [
                    drop_health_probe_access_logs,
                    drop_loggers("httpx"),
                ]
                if config.level > "debug"
                else [
                    structlog.processors.CallsiteParameterAdder(additional_ignores=["l2sl"]),
                ]
            ),
            structlog.contextvars.merge_contextvars,
            add_open_telemetry_spans,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.dev.set_exc_info,
            LazyValue.evaluate,
            *(
                [  # type: ignore[list-item]
                    structlog.processors.ExceptionRenderer(
                        structlog.processors.ExceptionDictTransformer(show_locals=show_locals, suppress=suppress_locals)
                    ),
                    structlog.processors.JSONRenderer(),
                ]
                if config.as_json
                else [
                    structlog.dev.ConsoleRenderer(
                        exception_formatter=structlog.dev.RichTracebackFormatter(
                            show_locals=show_locals, suppress=suppress_locals
                        )
                    ),
                ]
            ),
        ],
    )

    l2sl.configure_stdlib_log_forwarding()


def drop_health_probe_access_logs(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    if event_dict.get("logger") == "uvicorn.access" and event_dict["endpoint"] == "/health":
        raise structlog.DropEvent()

    return event_dict


def drop_loggers(*loggers: str) -> Processor:
    def drop_logs(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        if event_dict.get("logger") in loggers:
            raise structlog.DropEvent()

        return event_dict

    return drop_logs


def add_open_telemetry_spans(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    span = trace.get_current_span()
    if not span.is_recording():
        event_dict["span"] = None
        return event_dict

    ctx = span.get_span_context()
    parent = getattr(span, "parent", None)

    event_dict["span"] = {
        "span_id": format(ctx.span_id, "016x"),
        "trace_id": format(ctx.trace_id, "032x"),
        "parent_span_id": None if not parent else format(parent.span_id, "016x"),
    }

    return event_dict


class LazyValue:
    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory

    def __call__(self) -> Any:
        return self._factory()

    @staticmethod
    def evaluate(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        return {k: v() if isinstance(v, LazyValue) else v for k, v in event_dict.items()}
