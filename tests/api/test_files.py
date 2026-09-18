import base64
import mimetypes
import uuid
from urllib.parse import urlparse

import ag_ui.core
import compyre
import pydantic
import pytest
import pytest_httpserver.httpserver

from _ravnar.config import BaseConfig
from _ravnar.file_storage import RAVNAR_PROVIDER, FilePart


class TestFiles:
    @pytest.mark.parametrize("mime_type", ["application/octet-stream", "image/jpeg"])
    @pytest.mark.parametrize("metadata", [None, "metadata", {"foo": "bar"}])
    def test_e2e_data_source(self, app_client, mime_type, metadata):
        content = b"content"

        response = app_client.post(
            "/api/files",
            json=ag_ui.core.ImagePart(
                source=ag_ui.core.DataSource(value=base64.b64encode(content).decode(), mime_type=mime_type),
                metadata=metadata,
            ).model_dump(mode="json"),
        ).raise_for_status()
        file_part = pydantic.TypeAdapter(FilePart).validate_json(response.content)

        assert file_part.source.type == "file"
        assert file_part.source.provider == RAVNAR_PROVIDER
        assert file_part.source.mime_type == mime_type
        assert file_part.metadata == metadata

        file_id = uuid.UUID(file_part.source.value)

        expected = file_part
        response = app_client.get(f"/api/files/{file_id}").raise_for_status()
        actual = pydantic.TypeAdapter(FilePart).validate_json(response.content)
        compyre.assert_equal(actual, expected)

        response = app_client.get(f"/api/files/{file_id}/content").raise_for_status()
        assert response.content == content
        assert response.headers.get("Content-Type") == mime_type

    @pytest.fixture
    def url_app_client(self, httpserver, request):
        """Create a test client with URL source enabled and the test server's hostname allowlisted."""
        from tests.utils import TestClient

        parsed = urlparse(httpserver.url_for("/"))
        hostname = parsed.hostname or "localhost"
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

    @pytest.mark.parametrize("mime_type", [None, "image/jpeg", "application/octet-stream"])
    @pytest.mark.parametrize("source_content_type", [None, "image/png"])
    @pytest.mark.parametrize("metadata", [None, "metadata", {"foo": "bar"}])
    @pytest.mark.parametrize("endpoint", ["/image.jpg", "/file"])
    def test_e2e_url_source(self, url_app_client, httpserver, mime_type, source_content_type, metadata, endpoint):
        content = b"content"

        response_cls = pytest_httpserver.httpserver.Response
        response_cls.default_mimetype = None
        httpserver.expect_request(endpoint).respond_with_response(
            response_cls(content, content_type=source_content_type)
        )
        url = httpserver.url_for(endpoint)

        expected_mime_type = (
            mime_type or source_content_type or mimetypes.guess_type(url, strict=False)[0] or "application/octet-stream"
        )

        response = url_app_client.post(
            "/api/files",
            json=ag_ui.core.ImagePart(
                source=ag_ui.core.UrlSource(value=url, mime_type=mime_type), metadata=metadata
            ).model_dump(mode="json"),
        ).raise_for_status()
        file_part = pydantic.TypeAdapter(FilePart).validate_json(response.content)

        assert file_part.source.type == "file"
        assert file_part.source.provider == RAVNAR_PROVIDER
        assert file_part.source.mime_type == expected_mime_type
        assert file_part.metadata == metadata

        file_id = uuid.UUID(file_part.source.value)

        expected = file_part
        response = url_app_client.get(f"/api/files/{file_id}").raise_for_status()
        actual = pydantic.TypeAdapter(FilePart).validate_json(response.content)
        compyre.assert_equal(actual, expected)

        response = url_app_client.get(f"/api/files/{file_id}/content").raise_for_status()
        assert response.content == content
        assert response.headers.get("Content-Type") == expected_mime_type
