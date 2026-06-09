from __future__ import annotations

import urllib.parse

import pytest
from fastapi import HTTPException, status

from _ravnar.file_storage import FileHandler
from _ravnar.utils import normalize_hostname


class TestNormalizeHostname:
    @pytest.mark.parametrize(
        ("hostname", "expected"),
        [
            ("GITHUB.COM", "github.com"),
            ("Example.COM", "example.com"),
            ("München.example.com", "xn--mnchen-3ya.example.com"),
            ("xn--mnchen-3ya.example.com", "xn--mnchen-3ya.example.com"),
            ("93.184.216.34", "93.184.216.34"),
            ("github.com.", "github.com."),
        ],
    )
    def test_normalize(self, hostname: str, expected: str) -> None:
        assert normalize_hostname(hostname) == expected

    def test_invalid_idna_raises_value_error(self) -> None:
        # Double dot creates an empty label which is invalid in IDNA
        with pytest.raises(ValueError):
            normalize_hostname("example..com")


class TestValidateURL:
    """Tests for FileHandler._validate_url (a sync @staticmethod)."""

    @pytest.mark.parametrize(
        ("url", "allowed_hostnames"),
        [
            pytest.param("http://example.com/file", ["example.com"], id="exact_match"),
            pytest.param("http://sub.example.com/file", ["example.com"], id="subdomain_match"),
            pytest.param("http://example.com/file", ["example.com"], id="case_insensitive_match"),
            pytest.param("http://evil.com/file", ["*"], id="wildcard_allows_all"),
            pytest.param("http://169.254.169.254/latest/meta-data/", ["*"], id="wildcard_allows_internal"),
            pytest.param("http://user:pass@example.com/file", ["example.com"], id="url_with_userinfo"),
            pytest.param("http://example.com:8080/file", ["example.com"], id="url_with_non_standard_port"),
            pytest.param("http://93.184.216.34/file", ["93.184.216.34"], id="ip_literal_in_allowlist"),
            pytest.param(
                "http://MÜNCHEN.example.com/file",
                ["xn--mnchen-3ya.example.com"],
                id="idn_hostname_matching_idn_entry",
            ),
            pytest.param(
                "http://MÜNCHEN.example.com/file",
                ["xn--mnchen-3ya.example.com"],
                id="idn_hostname_matching_punycode_entry",
            ),
        ],
    )
    def test_allowed(self, url: str, allowed_hostnames: list[str]) -> None:
        assert FileHandler._validate_url(url, allowed_hostnames=allowed_hostnames) == url

    @pytest.mark.parametrize(
        ("url", "allowed_hostnames"),
        [
            pytest.param(
                "http://example.com/file",
                [],
                id="empty_allowlist",
            ),
            pytest.param("http://evil.com/file", ["example.com"], id="non_match"),
            pytest.param(
                "http://example.com./file",
                ["example.com"],
                id="hostname_trailing_dot_not_matching",
            ),
            pytest.param(
                "http://93.184.216.34/file",
                ["example.com"],
                id="ip_literal_not_in_allowlist",
            ),
            pytest.param("file:///etc/passwd", ["example.com"], id="url_with_no_hostname"),
        ],
    )
    def test_blocked(self, url: str, allowed_hostnames: list[str]) -> None:
        with pytest.raises(HTTPException) as exc_info:
            FileHandler._validate_url(url, allowed_hostnames=allowed_hostnames)
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST


class TestValidateURLIntegration:
    """Integration tests via HTTP client against a running ravnar instance."""

    @pytest.fixture
    def app_client(self, httpserver):
        from _ravnar.config import BaseConfig
        from tests.utils import TestClient

        hostname = urllib.parse.urlparse(httpserver.url_for("/")).hostname or "localhost"
        config = BaseConfig.model_validate(
            {
                "security": {
                    "authenticator": "tests.utils.HeaderAuthenticator",
                },
                "storage": {
                    "files": {
                        "url_data_source": {
                            "enabled": True,
                            "allowed_hostnames": [hostname],
                        },
                    },
                },
            }
        )
        with TestClient.from_config(config) as client:
            yield client

    def test_upload_from_allowlisted_url_succeeds(self, app_client, httpserver):
        httpserver.expect_request("/file.txt").respond_with_data(b"hello")
        url = httpserver.url_for("/file.txt")

        response = app_client.post(
            "/api/files",
            json={
                "type": "document",
                "source": {"type": "url", "value": url},
            },
        )
        assert response.status_code == status.HTTP_200_OK

    def test_upload_from_non_allowlisted_url_fails(self, app_client, httpserver):
        response = app_client.post(
            "/api/files",
            json={
                "type": "document",
                "source": {"type": "url", "value": "http://evil.com/malware"},
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_url_source_disabled_fails(self, app_client, httpserver):
        from _ravnar.config import BaseConfig
        from tests.utils import TestClient

        config = BaseConfig.model_validate(
            {
                "security": {
                    "authenticator": "tests.utils.HeaderAuthenticator",
                },
                "storage": {
                    "files": {
                        "url_data_source": {
                            "enabled": False,
                        },
                    },
                },
            }
        )
        with TestClient.from_config(config) as client:
            response = client.post(
                "/api/files",
                json={
                    "type": "document",
                    "source": {"type": "url", "value": "http://example.com/file"},
                },
            )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
