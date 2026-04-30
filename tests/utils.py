import contextlib
from typing import Annotated

from fastapi import Depends
from fastapi.security import APIKeyHeader
from fastapi.testclient import TestClient as _TestClient

from _ravnar import schema
from _ravnar.config import BaseConfig
from _ravnar.core import Ravnar
from ravnar.authenticators import Authenticator


class TestClient(_TestClient):
    config: BaseConfig

    @classmethod
    def from_config(cls, config):
        client = cls(Ravnar(config).app)
        client.config = config
        return client

    @property
    def any_agent_id(self):
        return next(iter(self.config.agents))


class ForwardedUserAuthenticator(Authenticator):
    """Forwarded User Authenticator"""

    async def authenticate(self, id: Annotated[str | None, Depends(APIKeyHeader(name="User", auto_error=False))]):
        return schema.User(id=id or "pytest")


@contextlib.contextmanager
def make_app_client(config=None):
    if config is None:
        config = BaseConfig.model_validate(
            {
                "security": {
                    "authenticator": ForwardedUserAuthenticator,
                },
            }
        )

    with TestClient.from_config(config) as client:
        assert client.get("/health").is_success
        yield client
