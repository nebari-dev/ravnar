from __future__ import annotations

import urllib.parse
from datetime import timedelta

import httpx
import pytest
import pytest_httpserver
from fastapi import HTTPException, status

import pydantic
from _ravnar.config import URLDataSourceConfig, FileStorageConfig, normalize_hostname


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
        config = URLDataSourceConfig(allowlist=["GITHUB.COM", "München.example.com"])
        assert config.allowlist == ["github.com", "xn--mnchen-3ya.example.com"]

    def test_wildcard_preserved(self) -> None:
        config = URLDataSourceConfig(allowlist=["*"])
        assert config.allowlist == ["*"]

    def test_invalid_allowlist_entry_raises(self) -> None:
        # Double dot creates an empty label which is invalid in IDNA
        with pytest.raises(pydantic.ValidationError):
            URLDataSourceConfig(allowlist=["example..com"])

    def test_timeout_default(self) -> None:
        config = URLDataSourceConfig()
        assert config.timeout == timedelta(seconds=30)

    def test_enabled_default(self) -> None:
        config = URLDataSourceConfig()
        assert config.enabled is False


def _make_handler(url_data_source_config: URLDataSourceConfig | None = None):
    """Create a FileHandler with the given URL data source config for testing."""
    from _ravnar.file_storage import FileHandler

    file_storage_config = FileStorageConfig(
        url_data_source=url_data_source_config or URLDataSourceConfig(enabled=True, allowlist=["example.com"])
    )
    handler = FileHandler.__new__(FileHandler)
    handler._file_storage_config = file_storage_config
    return handler


class TestValidateURL:
    def test_not_enabled(self) -> None:
        handler = _make_handler(URLDataSourceConfig(enabled=False))
        with pytest.raises(HTTPException) as exc_info:
            handler._validate_url("http://example.com/file")
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "URL file source is not enabled" in exc_info.value.detail

    def test_empty_allowlist_blocks_all(self) -> None:
        handler = _make_handler(URLDataSourceConfig(enabled=True, allowlist=[]))
        with pytest.raises(HTTPException) as exc_info:
            handler._validate_url("http://example.com/file")
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST

    def test_exact_match(self) -> None:
        handler = _make_handler()
        result = handler._validate_url("http://example.com/file")
        assert result == "http://example.com/file"

    def test_subdomain_match(self) -> None:
        handler = _make_handler(URLDataSourceConfig(enabled=True, allowlist=["example.com"]))
        result = handler._validate_url("http://sub.example.com/file")
        assert result == "http://sub.example.com/file"

    def test_case_insensitive_match(self) -> None:
        handler = _make_handler(URLDataSourceConfig(enabled=True, allowlist=["EXAMPLE.COM"]))
        result = handler._validate_url("http://example.com/file")
        assert result == "http://example.com/file"

    def test_non_match(self) -> None:
        handler = _make_handler(URLDataSourceConfig(enabled=True, allowlist=["example.com"]))
        with pytest.raises(HTTPException) as exc_info:
            handler._validate_url("http://evil.com/file")
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST

    def test_wildcard_allows_all(self) -> None:
        handler = _make_handler(URLDataSourceConfig(enabled=True, allowlist=["*"]))
        result = handler._validate_url("http://evil.com/file")
        assert result == "http://evil.com/file"

    def test_wildcard_allows_internal(self) -> None:
        handler = _make_handler(URLDataSourceConfig(enabled=True, allowlist=["*"]))
        result = handler._validate_url("http://169.254.169.254/latest/meta-data/")
        assert result == "http://169.254.169.254/latest/meta-data/"

    def test_url_with_userinfo(self) -> None:
        handler = _make_handler()
        result = handler._validate_url("http://user:pass@example.com/file")
        assert result == "http://user:pass@example.com/file"

    def test_url_with_non_standard_port(self) -> None:
        handler = _make_handler()
        result = handler._validate_url("http://example.com:8080/file")
        assert result == "http://example.com:8080/file"

    def test_hostname_trailing_dot_not_matching(self) -> None:
        handler = _make_handler(URLDataSourceConfig(enabled=True, allowlist=["example.com"]))
        with pytest.raises(HTTPException) as exc_info:
            handler._validate_url("http://example.com./file")
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST

    def test_ip_literal_not_in_allowlist(self) -> None:
        handler = _make_handler(URLDataSourceConfig(enabled=True, allowlist=["example.com"]))
        with pytest.raises(HTTPException) as exc_info:
            handler._validate_url("http://93.184.216.34/file")
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST

    def test_ip_literal_in_allowlist(self) -> None:
        handler = _make_handler(URLDataSourceConfig(enabled=True, allowlist=["93.184.216.34"]))
        result = handler._validate_url("http://93.184.216.34/file")
        assert result == "http://93.184.216.34/file"

    def test_url_with_no_hostname(self) -> None:
        handler = _make_handler()
        with pytest.raises(HTTPException) as exc_info:
            handler._validate_url("file:///etc/passwd")
        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST

    def test_idn_hostname_matching_idn_entry(self) -> None:
        handler = _make_handler(URLDataSourceConfig(enabled=True, allowlist=["münchen.example.com"]))
        result = handler._validate_url("http://MÜNCHEN.example.com/file")
        assert result == "http://MÜNCHEN.example.com/file"

    def test_idn_hostname_matching_punycode_entry(self) -> None:
        handler = _make_handler(URLDataSourceConfig(enabled=True, allowlist=["xn--mnchen-3ya.example.com"]))
        result = handler._validate_url("http://MÜNCHEN.example.com/file")
        assert result == "http://MÜNCHEN.example.com/file"


class TestValidateURLIntegration:
    """Integration tests via HTTP client against a running ravnar instance."""

    @pytest.fixture
    def app_client(self, httpserver):
        from tests.utils import TestClient
        from _ravnar.config import BaseConfig

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
                            "allowlist": [hostname],
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
        # Point to a URL on a non-allowlisted host
        response = app_client.post(
            "/api/files",
            json={
                "type": "document",
                "source": {"type": "url", "value": "http://evil.com/malware"},
            },
        )
        assert response.status_code == 400

    def test_url_source_disabled_fails(self, app_client, httpserver):
        """Override fixture to disable URL source."""
        from tests.utils import TestClient
        from _ravnar.config import BaseConfig

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
