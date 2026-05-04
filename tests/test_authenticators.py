import pytest
from fastapi import status

from _ravnar import schema
from _ravnar.authenticators import BearerTokenAuthenticator, ForwardedUserAuthenticator
from _ravnar.config import BaseConfig
from tests.utils import make_app_client


class TestForwardedUserAuthenticator:
    @pytest.fixture(scope="class")
    def client(self):
        config = BaseConfig.model_validate({"security": {"authenticator": ForwardedUserAuthenticator}})
        with make_app_client(config) as client:
            yield client

    def test_no_header(self, client):
        assert client.get("/api/user").status_code == status.HTTP_401_UNAUTHORIZED

    def test_forwarded_user(self, client):
        user_id = "user-id"

        response = client.get("/api/user", headers={"X-Forwarded-User": user_id}).raise_for_status()
        user = response.json()

        assert user["id"] == user_id


class TestBearerTokenAuthenticator:
    @pytest.fixture(scope="class")
    def client(self):
        config = BaseConfig.model_validate(
            {
                "security": {
                    "authenticator": {
                        "cls_or_fn": BearerTokenAuthenticator,
                        "params": {"token_validator": lambda token: schema.User(id=token)},
                    }
                }
            }
        )
        with make_app_client(config) as client:
            yield client

    @pytest.mark.parametrize(
        "authorization", [None, "plain_token", "non-bearer-scheme token", "something else entirely"]
    )
    def test_invalid_authorization_header(self, client, authorization):
        headers = {"Authorization": authorization} if authorization is not None else None
        assert client.get("/api/user", headers=headers).status_code == status.HTTP_401_UNAUTHORIZED

    def test_valid_token(self, client):
        token = "user-id"

        response = client.get("/api/user", headers={"Authorization": f"Bearer {token}"}).raise_for_status()
        user = response.json()

        assert user["id"] == token
