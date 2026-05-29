import compyre
import pydantic
import pytest
from fastapi import status

import ravnar.agents
from _ravnar import schema
from _ravnar.config import BaseConfig
from tests.utils import HeaderAuthenticator, make_app_client


def make_config(*, dynamic_enabled=False):
    return BaseConfig.model_validate(
        {
            "agents": {
                "static": {
                    "default": ravnar.agents.DefaultAgent,
                },
                "dynamic": {"enabled": dynamic_enabled},
            },
            "security": {
                "authenticator": HeaderAuthenticator,
            },
        }
    )


class TestDynamicAgentsDisabled:
    @pytest.fixture
    def client(self):
        with make_app_client(
            BaseConfig.model_validate(
                {
                    "agents": {
                        "static": {
                            "default": ravnar.agents.DefaultAgent,
                        },
                        "dynamic": {"enabled": False},
                    },
                    "security": {
                        "authenticator": HeaderAuthenticator,
                    },
                }
            )
        ) as c:
            yield c

    def test_get_agents(self, client):
        response = client.get("/api/agents").raise_for_status()
        actual = pydantic.TypeAdapter(list[schema.AgentInfo]).validate_json(response.content)
        expected = [
            schema.AgentInfo.from_agent(id, agent_factory())
            for id, agent_factory in client.config.agents.static.items()
        ]

        compyre.assert_equal(actual, expected)

    def test_get_config(self, client):
        response = client.get("/api/config").raise_for_status()
        config = schema.APIConfig.model_validate_json(response.content)
        assert config.dynamic_agents_enabled is False

    def test_register_agent_not_mounted(self, client):
        response = client.post(
            "/api/agents",
            json={
                "id": "dynamic",
                "agent": {"cls_or_fn": "ravnar.agents.DefaultAgent"},
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
        with make_app_client(
            BaseConfig.model_validate(
                {
                    "agents": {
                        "static": {
                            "default": {"cls_or_fn": "ravnar.agents.DefaultAgent"},
                        },
                        "dynamic": {"enabled": True},
                    },
                }
            )
        ) as c:
            yield c

    def test_get_agents_initial(self, client):
        response = client.get("/api/agents").raise_for_status()
        actual = pydantic.TypeAdapter(list[schema.AgentInfo]).validate_json(response.content)
        expected = [
            schema.AgentInfo.from_agent(id, agent_factory())
            for id, agent_factory in client.config.agents.static.items()
        ]

        compyre.assert_equal(actual, expected)

    def test_get_config(self, client):
        response = client.get("/api/config").raise_for_status()
        config = schema.APIConfig.model_validate_json(response.content)
        assert config.dynamic_agents_enabled is True

    def test_register_agent(self, client):
        id = "sentinel"

        response = client.post(
            "/api/agents",
            json={
                "id": id,
                "agent": {"cls_or_fn": "ravnar.agents.DefaultAgent"},
            },
        ).raise_for_status()
        info = schema.AgentInfo.model_validate_json(response.content)
        assert info.id == id

    def test_register_agent_appears_in_list(self, client):
        client.post(
            "/api/agents",
            json={
                "id": "listed-agent",
                "agent": {"cls_or_fn": "ravnar.agents.DefaultAgent"},
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
                "agent": {"cls_or_fn": "ravnar.agents.DefaultAgent"},
            },
        ).raise_for_status()

        response = client.post(
            "/api/agents",
            json={
                "id": "dup-agent",
                "agent": {"cls_or_fn": "ravnar.agents.DefaultAgent"},
            },
        )
        assert response.status_code == status.HTTP_409_CONFLICT

    def test_register_static_agent_id_returns_409(self, client):
        response = client.post(
            "/api/agents",
            json={
                "id": "default",
                "agent": {"cls_or_fn": "ravnar.agents.DefaultAgent"},
            },
        )
        assert response.status_code == status.HTTP_409_CONFLICT

    def test_unregister_dynamic_agent(self, client):
        client.post(
            "/api/agents",
            json={
                "id": "to-delete",
                "agent": {"cls_or_fn": "ravnar.agents.DefaultAgent"},
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
                "agent": {"cls_or_fn": "ravnar.agents.DefaultAgent"},
            },
        ).raise_for_status()

        client.delete("/api/agents/delete-twice").raise_for_status()

        response = client.delete("/api/agents/delete-twice")
        assert response.status_code == status.HTTP_404_NOT_FOUND
