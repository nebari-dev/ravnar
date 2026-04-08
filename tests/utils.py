import contextlib

from fastapi.testclient import TestClient

from _ravnar.config import BaseConfig
from _ravnar.core import Ravnar


@contextlib.contextmanager
def app_client(config=None):
    if config is None:
        config = BaseConfig()
    with TestClient(Ravnar(config).app) as client:
        assert client.get("/health").is_success
        yield client
