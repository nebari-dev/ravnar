from __future__ import annotations

import urllib.parse
from datetime import timedelta

import pydantic
import pytest
from fastapi import HTTPException, status

from _ravnar.config import URLDataSourceConfig, normalize_hostname
from _ravnar.file_storage import FileHandler


class TestNormalizeHostname:
    def test_ascii_lowercasing(self) -> None:
        assert normalize_hostname("GITHUB.COM") == "github.com"
        assert normalize_hostname("Example.COM") == "example.com"

    def test_unicode_to_punycode(self) -> None:
        assert normalize_hostname("München.example.com") == "xn--mnchen-3ya.example.com"

    def test_punycode_idempotent(self) -> None:
        result = normalize_hostname("xn--mnchen-3ya.example.com")
        assert result == "xn--mnchen-3ya.example.com"

    def test_invalid_idna_raises_value_error(self) -> None:
        # Double dot creates an empty label which is invalid in IDNA
        with pytest.raises(ValueError):
            normalize_hostname("example..com")

    def test_ipv4_passthrough(self) -> None:
        assert normalize_hostname("93.184.216.34") == "93.184.216.34"

    def test_trailing_dot_preserved(self) -> None:
        assert normalize_hostname("github.com.") == "github.com."


class TestURLDataSourceConfig:
    def test_allowlist_normalization(self) -> None:
        config = URLDataSourceConfig(allowed_hostnames=["GITHUB.COM", "München.example.com"])
        assert config.allowed_hostnames == ["github.com", "xn--mnchen-3ya.example.com"]

    def test_wildcard_preserved(self) -> None:
        config = URLDataSourceConfig(allowed_hostnames=["*"])
        assert config.allowed_hostnames == ["*"]

    def test_wildcard_with_others_blocked(self) -> None:
        with pytest.raises(pydantic.ValidationError) as exc_info:
            URLDataSourceConfig(allowed_hostnames=["*", "example.com"])
        assert "must be the sole" in str(exc_info.value)

    def test_invalid_allowlist_entry_raises(self) -> None:
        # Double dot creates an empty label which is invalid in IDNA
        with pytest.raises(pydantic.ValidationError):
            URLDataSourceConfig(allowed_hostnames=["example..com"])

    def test_timeout_default(self) -> None:
        config = URLDataSourceConfig()
        assert config.timeout == timedelta(seconds=30)

    def test_enabled_default(self) -> None:
        config = URLDataSourceConfig()
        assert config.enabled is False


class TestValidateURL:
    """Tests for FileHandler._validate_url (a sync @staticmethod)."""

    def test_not_enabled(self) -> None:
        """URL source not enabled is checked in _extract_url, not _validate_url.

        _validate_url only validates the hostname against the allowlist.
        When allowlist is empty, all URLs are rejected.
        """
        allowlist: list[str] = []
        with pytest.raises(HTTPException) as exc_info:
            FileHandler._validate_url("http://example.com/file", allowed_hostnames=allowlist)
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST

    def test_exact_match(self) -> None:
        result = FileHandler._validate_url("http://example.com/file", allowed_hostnames=["example.com"])
        assert result == "http://example.com/file"

    def test_subdomain_match(self) -> None:
        result = FileHandler._validate_url("http://sub.example.com/file", allowed_hostnames=["example.com"])
        assert result == "http://sub.example.com/file"

    def test_case_insensitive_match(self) -> None:
        # Allowlist entry is already normalized (lowercased) at config load time
        result = FileHandler._validate_url("http://example.com/file", allowed_hostnames=["example.com"])
        assert result == "http://example.com/file"

    def test_non_match(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            FileHandler._validate_url("http://evil.com/file", allowed_hostnames=["example.com"])
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST

    def test_wildcard_allows_all(self) -> None:
        result = FileHandler._validate_url("http://evil.com/file", allowed_hostnames=["*"])
        assert result == "http://evil.com/file"

    def test_wildcard_allows_internal(self) -> None:
        result = FileHandler._validate_url("http://169.254.169.254/latest/meta-data/", allowed_hostnames=["*"])
        assert result == "http://169.254.169.254/latest/meta-data/"

    def test_url_with_userinfo(self) -> None:
        result = FileHandler._validate_url("http://user:pass@example.com/file", allowed_hostnames=["example.com"])
        assert result == "http://user:pass@example.com/file"

    def test_url_with_non_standard_port(self) -> None:
        result = FileHandler._validate_url("http://example.com:8080/file", allowed_hostnames=["example.com"])
        assert result == "http://example.com:8080/file"

    def test_hostname_trailing_dot_not_matching(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            FileHandler._validate_url("http://example.com./file", allowed_hostnames=["example.com"])
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST

    def test_ip_literal_not_in_allowlist(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            FileHandler._validate_url("http://93.184.216.34/file", allowed_hostnames=["example.com"])
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST

    def test_ip_literal_in_allowlist(self) -> None:
        result = FileHandler._validate_url("http://93.184.216.34/file", allowed_hostnames=["93.184.216.34"])
        assert result == "http://93.184.216.34/file"

    def test_url_with_no_hostname(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            FileHandler._validate_url("file:///etc/passwd", allowed_hostnames=["example.com"])
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST

    def test_idn_hostname_matching_idn_entry(self) -> None:
        # Config normalizes the IDN allowlist entry at load time
        result = FileHandler._validate_url(
            "http://MÜNCHEN.example.com/file",
            allowed_hostnames=["xn--mnchen-3ya.example.com"],
        )
        assert result == "http://MÜNCHEN.example.com/file"

    def test_idn_hostname_matching_punycode_entry(self) -> None:
        result = FileHandler._validate_url(
            "http://MÜNCHEN.example.com/file",
            allowed_hostnames=["xn--mnchen-3ya.example.com"],
        )
        assert result == "http://MÜNCHEN.example.com/file"


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
        assert response.status_code == 200

    def test_upload_from_non_allowlisted_url_fails(self, app_client, httpserver):
        response = app_client.post(
            "/api/files",
            json={
                "type": "document",
                "source": {"type": "url", "value": "http://evil.com/malware"},
            },
        )
        assert response.status_code == 400

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
        assert response.status_code == 400
