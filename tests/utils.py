import contextlib

from fastapi.testclient import TestClient as _TestClient

from _ravnar.config import BaseConfig
from _ravnar.core import Ravnar


class TestClient(_TestClient):
    config: BaseConfig

    @classmethod
    def from_config(cls, config=None):
        if config is None:
            config = BaseConfig()
        client = cls(Ravnar(config).app)
        client.config = config
        return client

    @property
    def any_agent_id(self):
        return next(iter(self.config.agents))


@contextlib.contextmanager
def app_client(config=None):
    with TestClient.from_config(config) as client:
        assert client.get("/health").is_success
        yield client
