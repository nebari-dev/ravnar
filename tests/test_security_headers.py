from __future__ import annotations

import pytest
from fastapi import status
from starlette.testclient import TestClient

from _ravnar.core import Ravnar
from _ravnar.config import BaseConfig
from tests.utils import TestClient, HeaderAuthenticator

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "content-security-policy": "default-src 'none'",
}


@pytest.fixture
def client():
    config = BaseConfig.model_validate(
        {
            "security": {
                "authenticator": HeaderAuthenticator,
            },
        }
    )
    with TestClient.from_config(config) as client:
        assert client.get("/health").is_success
        yield client


class TestSecurityHeaders:
    """Security headers are present on all responses."""

    @pytest.mark.parametrize(
        "endpoint,expected_status",
        [
            ("/health", status.HTTP_200_OK),
            ("/version", status.HTTP_200_OK),
            ("/nonexistent", status.HTTP_404_NOT_FOUND),
            ("/api/user", status.HTTP_200_OK),
        ],
    )
    def test_security_headers_present(self, client: TestClient, endpoint: str, expected_status: int):
        response = client.get(endpoint)
        assert response.status_code == expected_status

        for header_name, expected_value in SECURITY_HEADERS.items():
            assert header_name in response.headers, f"Missing header: {header_name}"
            assert response.headers[header_name] == expected_value

    def test_headers_not_overwritten_when_already_set(self, client: TestClient):
        """If a response already sets one of the security headers, the middleware should not overwrite it."""
        # Set up a route that already sets one of the security headers
        @client.app.get("/custom-header")
        def custom_header_endpoint():
            from fastapi.responses import Response
            resp = Response("ok")
            resp.headers["X-Content-Type-Options"] = "some-other-value"
            return resp

        response = client.get("/custom-header")
        # Our endpoint-set value should be preserved since the middleware uses setdefault
        assert response.headers["x-content-type-options"] == "some-other-value"

    def test_no_duplicate_headers(self, client: TestClient):
        """Ensure headers appear only once."""
        response = client.get("/health")
        for header_name in SECURITY_HEADERS:
            values = response.headers.get_list(header_name)
            assert len(values) == 1, f"Header {header_name} has {len(values)} values: {values}"
