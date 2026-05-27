import contextlib
import re
import uuid
from datetime import UTC, date, datetime, timedelta
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


class Sentinels:
    def __init__(self):
        self._uuids = 0
        self._ids = 0
        self._timestamps = 0

    def new_uuid(self):
        self._uuids += 1
        return uuid.UUID(int=self._uuids)

    @staticmethod
    def is_uuid_sentinel(obj):
        if not isinstance(obj, uuid.UUID):
            return False

        return int(obj) < 2**12

    def new_id(self) -> str:
        self._ids += 1
        return f"sentinel:{self._ids}"

    @staticmethod
    def is_id_sentinel(obj):
        if not isinstance(obj, str):
            return False

        return re.match(r"^sentinel:\d+$", obj) is not None

    def new_datetime(self):
        self._timestamps += 1
        return datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC) + timedelta(seconds=self._timestamps)

    @staticmethod
    def is_datetime_sentinel(obj):
        if not isinstance(obj, datetime):
            return False

        return obj.date() == date(1970, 1, 1)
