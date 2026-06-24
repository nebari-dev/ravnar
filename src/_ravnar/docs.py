from typing import Any

from fastapi.testclient import TestClient

from _ravnar.core import Ravnar

from .config import BaseConfig

_CLIENT: TestClient | None = None


def Client(config: Any = None) -> TestClient:
    global _CLIENT
    if _CLIENT is not None:
        _CLIENT.__exit__(None, None, None)

    _CLIENT = TestClient(Ravnar(BaseConfig.model_validate(config or {})).app)
    # context needs to be entered here to trigger the lifespan events
    _CLIENT.__enter__()
    return _CLIENT
