import base64
import uuid

import ag_ui.core
import compyre
import httpx_sse
import pydantic
import pytest
from fastapi import status

from _ravnar import schema


class TestThreadsCRUD:
    @pytest.mark.parametrize("id", ["thread_id", None])
    @pytest.mark.parametrize("name", ["thread_name", None])
    def test_create_thread(self, app_client, id, name):
        agent_id = app_client.any_agent_id

        payload = {"agentId": agent_id, "name": name}
        if id is not None:
            payload["id"] = id

        response = app_client.post("/api/threads", json=payload).raise_for_status()
        thread = schema.Thread.model_validate_json(response.content)

        if id is None:
            pydantic.TypeAdapter(uuid.UUID).validate_python(thread.id)
        else:
            assert thread.id == id
        assert thread.name == name
        assert thread.agent_id == agent_id
        assert thread.updated_at == thread.created_at

        expected = thread
        response = app_client.get(f"/api/threads/{thread.id}").raise_for_status()
        actual = schema.Thread.model_validate_json(response.content)
        compyre.assert_equal(actual, expected)

    def test_create_thread_existing(self, app_client):
        id = "id"
        response = app_client.post(
            "/api/threads",
            json={"id": id, "agentId": app_client.any_agent_id},
        ).raise_for_status()
        thread = schema.Thread.model_validate_json(response.content)

        response = app_client.post(
            "/api/threads",
            json={"id": id, "agentId": app_client.any_agent_id},
        )
        assert response.status_code == status.HTTP_409_CONFLICT

        expected = thread
        response = app_client.get(f"/api/threads/{thread.id}").raise_for_status()
        actual = schema.Thread.model_validate_json(response.content)
        compyre.assert_equal(actual, expected)

    def test_create_thread_existing_not_owned(self, app_client):
        id = "id"
        response = app_client.post(
            "/api/threads", json={"id": id, "agentId": app_client.any_agent_id}, headers={"User": "A"}
        ).raise_for_status()
        thread = schema.Thread.model_validate_json(response.content)

        response = app_client.post(
            "/api/threads", json={"id": id, "agentId": app_client.any_agent_id}, headers={"User": "B"}
        )
        assert response.status_code == status.HTTP_409_CONFLICT

        expected = thread
        response = app_client.get(f"/api/threads/{thread.id}", headers={"User": "A"}).raise_for_status()
        actual = schema.Thread.model_validate_json(response.content)
        compyre.assert_equal(actual, expected)

    def test_get_thread_non_existing(self, app_client):
        response = app_client.get("/api/threads/non-existing")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_thread_not_owned(self, app_client):
        response = app_client.post(
            "/api/threads", json={"agentId": app_client.any_agent_id}, headers={"User": "A"}
        ).raise_for_status()
        thread = schema.Thread.model_validate_json(response.content)

        expected = thread
        response = app_client.get(f"/api/threads/{thread.id}", headers={"User": "A"}).raise_for_status()
        actual = schema.Thread.model_validate_json(response.content)
        compyre.assert_equal(actual, expected)

        response = app_client.get(f"/api/threads/{thread.id}", headers={"User": "B"})
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_threads(self, app_client):
        expected = []
        for idx in range(3):
            response = app_client.post(
                "/api/threads", json={"id": str(idx), "agentId": app_client.any_agent_id}, headers={"user": "A"}
            ).raise_for_status()
            expected.append(schema.Thread.model_validate_json(response.content))

        app_client.post(
            "/api/threads", json={"agentId": app_client.any_agent_id}, headers={"user": "B"}
        ).raise_for_status()

        response = app_client.get(
            "/api/threads", params={"sortBy": "createdAt", "sortOrder": "ascending"}, headers={"user": "A"}
        ).raise_for_status()
        actual = schema.Page[schema.Thread].model_validate_json(response.content).items

        compyre.assert_equal(actual, expected)

    @pytest.mark.parametrize("initial_name", ["initial_name", None])
    def test_rename_thread(self, app_client, initial_name):
        response = app_client.post(
            "/api/threads", json={"name": initial_name, "agentId": app_client.any_agent_id}
        ).raise_for_status()
        thread = schema.Thread.model_validate_json(response.content)

        name = "renamed_name"
        expected = thread.model_copy(update={"name": name})

        response = app_client.post(f"/api/threads/{thread.id}/rename", json={"name": name}).raise_for_status()
        actual = schema.Thread.model_validate_json(response.content)
        compyre.assert_equal(actual, expected)

        response = app_client.get(f"/api/threads/{thread.id}").raise_for_status()
        actual = schema.Thread.model_validate_json(response.content)
        compyre.assert_equal(actual, expected)

    def test_rename_thread_non_existing(self, app_client):
        response = app_client.post("/api/threads/non-existing/rename", json={"name": "renamed_name"})
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_rename_thread_not_owned(self, app_client):
        response = app_client.post(
            "/api/threads", json={"agentId": app_client.any_agent_id}, headers={"User": "A"}
        ).raise_for_status()
        thread = schema.Thread.model_validate_json(response.content)

        response = app_client.post(
            f"/api/threads/{thread.id}/rename", json={"name": "renamed_name"}, headers={"User": "B"}
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

        expected = thread
        response = app_client.get(f"/api/threads/{thread.id}", headers={"User": "A"}).raise_for_status()
        actual = schema.Thread.model_validate_json(response.content)
        compyre.assert_equal(actual, expected)

    @pytest.mark.parametrize(
        ("ids", "ids_to_delete"),
        [
            (["a", "b", "c"], ["a"]),
            (["a", "b", "c"], ["a", "c"]),
            (["a", "b", "c"], ["a", "b", "c"]),
        ],
    )
    def test_delete_threads(self, app_client, ids, ids_to_delete):
        assert set(ids_to_delete).issubset(ids)

        expected = []
        for id in ids:
            response = app_client.post(
                "/api/threads", json={"id": id, "agentId": app_client.any_agent_id}
            ).raise_for_status()
            thread = schema.Thread.model_validate_json(response.content)
            if thread.id not in ids_to_delete:
                expected.append(thread)

        app_client.request("DELETE", "/api/threads", json={"ids": ids_to_delete}).raise_for_status()

        response = app_client.get(
            "/api/threads", params={"sortBy": "createdAt", "sortOrder": "ascending"}
        ).raise_for_status()
        actual = schema.Page[schema.Thread].model_validate_json(response.content).items
        compyre.assert_equal(actual, expected)

    def test_delete_threads_non_existing(self, app_client):
        ids = ["a", "b", "c"]

        expected = []
        for id in ids:
            response = app_client.post(
                "/api/threads", json={"id": id, "agentId": app_client.any_agent_id}
            ).raise_for_status()
            expected.append(schema.Thread.model_validate_json(response.content))

        response = app_client.request("DELETE", "/api/threads", json={"ids": [*ids, "non-existing"]})
        assert response.status_code == status.HTTP_404_NOT_FOUND

        response = app_client.get(
            "/api/threads", params={"sortBy": "createdAt", "sortOrder": "ascending"}
        ).raise_for_status()
        actual = schema.Page[schema.Thread].model_validate_json(response.content).items
        compyre.assert_equal(actual, expected)

    def test_delete_threads_not_owned(self, app_client):
        expected_by_user = {"A": [], "B": []}
        for user, id in [("A", "1"), ("A", "2"), ("B", "3")]:
            response = app_client.post(
                "/api/threads", json={"id": id, "agentId": app_client.any_agent_id}, headers={"User": user}
            ).raise_for_status()
            expected_by_user[user].append(schema.Thread.model_validate_json(response.content))

        response = app_client.request(
            "DELETE", "/api/threads", json={"ids": [t.id for _, threads in expected_by_user.items() for t in threads]}
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

        user = "A"
        expected = expected_by_user[user]
        response = app_client.get(
            "/api/threads", params={"sortBy": "createdAt", "sortOrder": "ascending"}, headers={"User": user}
        ).raise_for_status()
        actual = schema.Page[schema.Thread].model_validate_json(response.content).items
        compyre.assert_equal(actual, expected)

        user = "B"
        expected = expected_by_user[user]
        response = app_client.get(
            "/api/threads", params={"sortBy": "createdAt", "sortOrder": "ascending"}, headers={"User": user}
        ).raise_for_status()
        actual = schema.Page[schema.Thread].model_validate_json(response.content).items
        compyre.assert_equal(actual, expected)

    def test_delete_thread(self, app_client):
        response = app_client.post("/api/threads", json={"agentId": app_client.any_agent_id}).raise_for_status()
        thread = schema.Thread.model_validate_json(response.content)

        app_client.delete(f"/api/threads/{thread.id}").raise_for_status()

        response = app_client.get(f"/api/threads/{thread.id}")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_thread_non_existing(self, app_client):
        response = app_client.delete("/api/threads/non-existing")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_thread_not_owned(self, app_client):
        response = app_client.post(
            "/api/threads", json={"agentId": app_client.any_agent_id}, headers={"User": "A"}
        ).raise_for_status()
        thread = schema.Thread.model_validate_json(response.content)

        response = app_client.delete(f"/api/threads/{thread.id}", headers={"User": "B"})
        assert response.status_code == status.HTTP_404_NOT_FOUND

        expected = thread
        response = app_client.get(f"/api/threads/{thread.id}", headers={"User": "A"})
        actual = schema.Thread.model_validate_json(response.content)
        compyre.assert_equal(actual, expected)


class TestThreadsCreateRun:
    def create_thread(self, app_client, **kwargs) -> schema.Thread:
        response = app_client.post("/api/threads", json={"agentId": app_client.any_agent_id}).raise_for_status()
        return schema.Thread.model_validate_json(response.content)

    def create_run(self, client, *, thread_id, data, **kwargs):
        ta = pydantic.TypeAdapter(ag_ui.core.Event)

        with httpx_sse.connect_sse(
            client,
            "POST",
            f"/api/threads/{thread_id}/run",
            json=schema.CreateRunData.model_validate(data).model_dump(mode="json", by_alias=True, exclude_unset=True),
            **kwargs,
        ) as event_source:
            event_source.response.raise_for_status()
            for sse in event_source.iter_sse():
                yield ta.validate_json(sse.data)

    def test_implicit_file_upload_smoke(self, app_client):
        thread_id = self.create_thread(app_client).id

        event_stream = self.create_run(
            app_client,
            thread_id=thread_id,
            data={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "data",
                                    "value": base64.b64encode(b"content").decode(),
                                    "mimeType": "image/jpeg",
                                },
                            }
                        ],
                    }
                ]
            },
        )
        list(event_stream)

        app_client.get(f"/api/threads/{thread_id}/messages").raise_for_status()

    def test_files_smoke(self, app_client):
        response = app_client.post(
            "/api/files",
            json={
                "type": "image",
                "source": {
                    "type": "data",
                    "value": base64.b64encode(b"content").decode(),
                    "mimeType": "image/jpeg",
                },
            },
        ).raise_for_status()
        file_input_content = response.json()

        thread_id = self.create_thread(app_client).id

        event_stream = self.create_run(
            app_client,
            thread_id=thread_id,
            data={"messages": [{"role": "user", "content": [file_input_content]}]},
        )
        list(event_stream)

        event_stream = self.create_run(
            app_client,
            thread_id=thread_id,
            data={"messages": [{"role": "user", "content": [{"type": "text", "text": "question 2"}]}]},
        )
        list(event_stream)

        app_client.get(f"/api/threads/{thread_id}/messages").raise_for_status()
