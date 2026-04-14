import compyre
import pytest

from _ravnar import schema
from tests.utils import app_client


class TestThreads:
    @pytest.fixture(scope="class")
    def client(self):
        with app_client() as client:
            yield client

    @pytest.mark.parametrize("initial_name", ["initial_name", None])
    def test_rename_thread(self, client, initial_name):
        response = client.post(
            "/api/threads", json={"name": initial_name, "agentId": client.any_agent_id}
        ).raise_for_status()
        thread = schema.Thread.model_validate_json(response.content)

        name = "renamed_name"
        expected = thread.model_copy(update={"name": name})

        response = client.post(f"/api/threads/{thread.id}/rename", json={"name": name}).raise_for_status()
        actual = schema.Thread.model_validate_json(response.content)
        compyre.assert_equal(actual, expected)

        response = client.get(f"/api/threads/{thread.id}").raise_for_status()
        actual = schema.Thread.model_validate_json(response.content)
        compyre.assert_equal(actual, expected)
