import ag_ui.core
import pydantic
import pytest
from fastapi import status

from _ravnar import schema
from _ravnar.config import BaseConfig
from tests.utils import ForwardedUserAuthenticator, TestClient


class TestStatelessMode:
    @pytest.fixture
    def stateless_config(self):
        return BaseConfig.model_validate(
            {
                "security": {
                    "authenticator": ForwardedUserAuthenticator,
                },
                "storage": {
                    "enabled": False,
                },
            }
        )

    @pytest.fixture
    def stateless_client(self, stateless_config):
        with TestClient.from_config(stateless_config) as client:
            yield client

    def test_config_reports_storage_disabled(self, stateless_client):
        response = stateless_client.get("/api/config").raise_for_status()
        config = schema.APIConfig.model_validate_json(response.content)
        assert config.storage_enabled is False

    def test_threads_returns_404(self, stateless_client):
        response = stateless_client.get("/api/threads")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_create_thread_returns_404(self, stateless_client):
        response = stateless_client.post("/api/threads", json={"agentId": "default"})
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_thread_returns_404(self, stateless_client):
        response = stateless_client.get("/api/threads/some-id")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_files_returns_404(self, stateless_client):
        response = stateless_client.post(
            "/api/files",
            json={
                "type": "image",
                "source": {
                    "type": "data",
                    "value": "Y29udGVudA==",
                    "mimeType": "image/jpeg",
                },
            },
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_file_returns_404(self, stateless_client):
        response = stateless_client.get("/api/files/some-id")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_file_content_returns_404(self, stateless_client):
        response = stateless_client.get("/api/files/some-id/content")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_file_returns_404(self, stateless_client):
        response = stateless_client.delete("/api/files/some-id")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_agent_run_endpoint_exists(self, stateless_client):
        """Verify the agents run endpoint is registered in stateless mode.
        We only check the route exists (not 404) — actual agent execution
        is tested in other test suites."""
        response = stateless_client.post(
            "/api/agents/default/run",
            content=b"{}",
            headers={"Content-Type": "application/json"},
        )
        # 422 means the route exists but payload was invalid
        # (404 would mean the route wasn't registered)
        assert response.status_code != status.HTTP_404_NOT_FOUND

    def test_user_endpoint_works(self, stateless_client):
        response = stateless_client.get("/api/user").raise_for_status()
        user = schema.User.model_validate_json(response.content)
        assert user.id == "pytest"

    def test_health_endpoint_works(self, stateless_client):
        response = stateless_client.get("/health")
        assert response.status_code == status.HTTP_200_OK


class TestStatefulMode:
    def test_config_reports_storage_enabled(self, app_client):
        response = app_client.get("/api/config").raise_for_status()
        config = schema.APIConfig.model_validate_json(response.content)
        assert config.storage_enabled is True
