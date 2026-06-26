from typing import Any

from fastapi.testclient import TestClient

from _ravnar.core import Ravnar
from _ravnar.security import User

from .config import BaseConfig

User._current_user = staticmethod(lambda: "Huginn")  # type: ignore[method-assign, assignment]

_CLIENT: TestClient | None = None


def Client(config: Any = None) -> TestClient:
    global _CLIENT
    if _CLIENT is not None:
        _CLIENT.__exit__(None, None, None)

    _CLIENT = TestClient(Ravnar(BaseConfig.model_validate(config or {})).app)
    # context needs to be entered here to trigger the lifespan events
    _CLIENT.__enter__()
    return _CLIENT
