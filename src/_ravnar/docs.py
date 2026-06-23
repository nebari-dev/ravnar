from typing import Any

import l2sl
from fastapi.testclient import TestClient

from _ravnar.core import Ravnar

from .config import BaseConfig

_CLIENT: TestClient | None = None


def Client(config: Any = None) -> TestClient:
    global _CLIENT
    if _CLIENT is not None:
        _CLIENT.__exit__(None, None, None)

    config = BaseConfig.model_validate(config or {})
    # Keep the executed-docs output focused on each example: silence ravnar's
    # runtime logging (httpx request logs, repeated OTel "already instrumented"
    # warnings from building a fresh app per cell) that nbconvert would
    # otherwise capture as cell output.
    config.observability.logging.level = l2sl.LogLevel("error")

    _CLIENT = TestClient(Ravnar(config).app)
    # context needs to be entered here to trigger the lifespan events
    _CLIENT.__enter__()
    return _CLIENT
