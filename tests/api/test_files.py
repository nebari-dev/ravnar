import base64
import mimetypes

import ag_ui.core
import compyre
import pydantic
import pytest
import pytest_httpserver.httpserver

from _ravnar import schema


class TestFiles:
    @pytest.mark.parametrize("mime_type", ["application/octet-stream", "image/jpeg"])
    @pytest.mark.parametrize("metadata", [None, "metadata", {"foo": "bar"}])
    def test_e2e_data_source(self, app_client, mime_type, metadata):
        content = b"content"

        response = app_client.post(
            "/api/files",
            json=ag_ui.core.ImageInputContent(
                source=ag_ui.core.InputContentDataSource(value=base64.b64encode(content).decode(), mime_type=mime_type),
                metadata=metadata,
            ).model_dump(mode="json"),
        ).raise_for_status()
        ravnar_file_input_content = pydantic.TypeAdapter(schema.RavnarFileInputContent).validate_json(response.content)

        assert ravnar_file_input_content.source.value.source_type == "data"
        assert ravnar_file_input_content.source.value.mime_type == mime_type
        assert ravnar_file_input_content.metadata == metadata

        file_id = ravnar_file_input_content.source.value.file_id

        expected = ravnar_file_input_content
        response = app_client.get(f"/api/files/{file_id}").raise_for_status()
        actual = pydantic.TypeAdapter(schema.RavnarFileInputContent).validate_json(response.content)
        compyre.assert_equal(actual, expected)

        response = app_client.get(f"/api/files/{file_id}/content").raise_for_status()
        assert response.content == content
        assert response.headers.get("Content-Type") == mime_type

    @pytest.mark.parametrize("mime_type", [None, "image/jpeg", "application/octet-stream"])
    @pytest.mark.parametrize("source_content_type", [None, "image/png"])
    @pytest.mark.parametrize("metadata", [None, "metadata", {"foo": "bar"}])
    @pytest.mark.parametrize("endpoint", ["/image.jpg", "/file"])
    def test_upload_file_url_source_with_mime_type(
        self, app_client, httpserver, mime_type, source_content_type, metadata, endpoint
    ):
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

        response = app_client.post(
            "/api/files",
            json=ag_ui.core.ImageInputContent(
                source=ag_ui.core.InputContentUrlSource(value=url, mime_type=mime_type), metadata=metadata
            ).model_dump(mode="json"),
        ).raise_for_status()
        ravnar_file_input_content = pydantic.TypeAdapter(schema.RavnarFileInputContent).validate_json(response.content)

        assert ravnar_file_input_content.source.value.mime_type == expected_mime_type
        assert ravnar_file_input_content.source.value.source_type == "url"
        assert ravnar_file_input_content.source.value.source_data == {"url": url}
        assert ravnar_file_input_content.metadata == metadata

        file_id = ravnar_file_input_content.source.value.file_id

        expected = ravnar_file_input_content
        response = app_client.get(f"/api/files/{file_id}").raise_for_status()
        actual = pydantic.TypeAdapter(schema.RavnarFileInputContent).validate_json(response.content)
        compyre.assert_equal(actual, expected)

        response = app_client.get(f"/api/files/{file_id}/content").raise_for_status()
        assert response.content == content
        assert response.headers.get("Content-Type") == expected_mime_type
