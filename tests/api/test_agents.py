import pytest
from fastapi import status

from _ravnar import schema
from _ravnar.config import BaseConfig
from tests.utils import make_app_client


def _make_config(*, dynamic_enabled=False):
    return BaseConfig.model_validate(
        {
            "agents": {
                "static": {
                    "default": {"cls_or_fn": "_ravnar.agents.DefaultAgent"},
                },
                "dynamic": {"enabled": dynamic_enabled},
            },
            "security": {
                "authenticator": "tests.utils.ForwardedUserAuthenticator",
            },
        }
    )


class TestDynamicAgentsDisabled:
    @pytest.fixture
    def client(self):
        with make_app_client(_make_config(dynamic_enabled=False)) as c:
            yield c

    def test_get_agents(self, client):
        response = client.get("/api/agents").raise_for_status()
        agents = [schema.AgentInfo.model_validate(a) for a in response.json()]
        assert len(agents) == 1
        assert agents[0].id == "default"

    def test_get_config(self, client):
        response = client.get("/api/config").raise_for_status()
        config = schema.APIConfig.model_validate_json(response.content)
        assert config.dynamic_agents_enabled is False

    def test_register_agent_not_mounted(self, client):
        response = client.post(
            "/api/agents",
            json={
                "id": "test-agent",
                "agent": {"cls_or_fn": "_ravnar.agents.DefaultAgent"},
            },
        )
        # 405 (Method Not Allowed) because GET /api/agents exists at that path,
        # but POST is not mounted when dynamic agents are disabled.
        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    def test_unregister_agent_not_mounted(self, client):
        response = client.delete("/api/agents/test-agent")
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestDynamicAgentsEnabled:
    @pytest.fixture
    def client(self):
        with make_app_client(_make_config(dynamic_enabled=True)) as c:
            yield c

    def test_get_agents_initial(self, client):
        response = client.get("/api/agents").raise_for_status()
        agents = [schema.AgentInfo.model_validate(a) for a in response.json()]
        assert len(agents) == 1
        assert agents[0].id == "default"

    def test_get_config(self, client):
        response = client.get("/api/config").raise_for_status()
        config = schema.APIConfig.model_validate_json(response.content)
        assert config.dynamic_agents_enabled is True

    def test_register_agent(self, client):
        response = client.post(
            "/api/agents",
            json={
                "id": "my-sse-agent",
                "agent": {"cls_or_fn": "_ravnar.agents.DefaultAgent"},
            },
        ).raise_for_status()
        info = schema.AgentInfo.model_validate_json(response.content)
        assert info.id == "my-sse-agent"

    def test_register_agent_appears_in_list(self, client):
        client.post(
            "/api/agents",
            json={
                "id": "listed-agent",
                "agent": {"cls_or_fn": "_ravnar.agents.DefaultAgent"},
            },
        ).raise_for_status()

        response = client.get("/api/agents").raise_for_status()
        agents = [schema.AgentInfo.model_validate(a) for a in response.json()]
        ids = [a.id for a in agents]
        assert "default" in ids
        assert "listed-agent" in ids

    def test_register_duplicate_id_returns_409(self, client):
        client.post(
            "/api/agents",
            json={
                "id": "dup-agent",
                "agent": {"cls_or_fn": "_ravnar.agents.DefaultAgent"},
            },
        ).raise_for_status()

        response = client.post(
            "/api/agents",
            json={
                "id": "dup-agent",
                "agent": {"cls_or_fn": "_ravnar.agents.DefaultAgent"},
            },
        )
        assert response.status_code == status.HTTP_409_CONFLICT

    def test_register_static_agent_id_returns_409(self, client):
        response = client.post(
            "/api/agents",
            json={
                "id": "default",
                "agent": {"cls_or_fn": "_ravnar.agents.DefaultAgent"},
            },
        )
        assert response.status_code == status.HTTP_409_CONFLICT

    def test_unregister_dynamic_agent(self, client):
        client.post(
            "/api/agents",
            json={
                "id": "to-delete",
                "agent": {"cls_or_fn": "_ravnar.agents.DefaultAgent"},
            },
        ).raise_for_status()

        response = client.delete("/api/agents/to-delete")
        response.raise_for_status()

        response = client.get("/api/agents").raise_for_status()
        agents = [schema.AgentInfo.model_validate(a) for a in response.json()]
        ids = [a.id for a in agents]
        assert "to-delete" not in ids

    def test_unregister_static_agent_returns_403(self, client):
        response = client.delete("/api/agents/default")
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_unregister_nonexistent_agent_returns_404(self, client):
        response = client.delete("/api/agents/nonexistent")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_unregister_twice_returns_404(self, client):
        client.post(
            "/api/agents",
            json={
                "id": "delete-twice",
                "agent": {"cls_or_fn": "_ravnar.agents.DefaultAgent"},
            },
        ).raise_for_status()

        client.delete("/api/agents/delete-twice").raise_for_status()

        response = client.delete("/api/agents/delete-twice")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_registered_agent_can_run_via_thread(self, client):
        client.post(
            "/api/agents",
            json={
                "id": "runnable-agent",
                "agent": {"cls_or_fn": "_ravnar.agents.DefaultAgent"},
            },
        ).raise_for_status()

        response = client.post(
            "/api/threads",
            json={"agentId": "runnable-agent"},
        ).raise_for_status()
        thread = schema.Thread.model_validate_json(response.content)

        import httpx_sse

        with httpx_sse.connect_sse(
            client,
            "POST",
            f"/api/threads/{thread.id}/run",
            json={"messages": [{"role": "user", "content": "hello"}]},
        ) as event_source:
            event_source.response.raise_for_status()
            events = list(event_source.iter_sse())
            assert len(events) > 0
