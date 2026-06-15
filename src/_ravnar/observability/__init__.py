from __future__ import annotations

__all__ = ["LazyValue", "StructlogSpanExporter", "configure", "traced"]

from typing import TYPE_CHECKING

from .logging import LazyValue
from .logging import configure_logging as configure_logging
from .tracing import StructlogSpanExporter, traced
from .tracing import configure as configure_tracing

if TYPE_CHECKING:
    from _ravnar.config import ObservabilityConfig


def configure(config: ObservabilityConfig) -> None:
    configure_logging(config.logging)
    configure_tracing(config.tracing)
