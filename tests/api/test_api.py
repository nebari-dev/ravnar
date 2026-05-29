import dataclasses

import pytest
from fastapi import status

from _ravnar import schema
from _ravnar.auth import ALL_PERMISSIONS
from _ravnar.config import BaseConfig
from tests.utils import ForwardedUserAuthenticator, make_app_client


@pytest.mark.parametrize("storage_enabled", [True, False])
def test_storage_enabled(storage_enabled):
    with make_app_client(config=BaseConfig.model_validate({"storage": {"enabled": storage_enabled}})) as client:
        response = client.get("/api/config").raise_for_status()
        config = schema.APIConfig.model_validate_json(response.content)
        assert config.storage_enabled is storage_enabled


@dataclasses.dataclass(kw_only=True, frozen=True)
class AuthorizationCase:
    method: str
    endpoint: str
    required_permissions: list[str]


@pytest.mark.parametrize(
    "case",
    [
        AuthorizationCase(method="GET", endpoint="/api/agents", required_permissions=["agents:read"]),
        AuthorizationCase(method="POST", endpoint="/api/agents", required_permissions=["agents:write"]),
        AuthorizationCase(method="POST", endpoint="/api/agents/{agentId}/run", required_permissions=["agents:read"]),
        AuthorizationCase(method="DELETE", endpoint="/api/agents/{agentId}", required_permissions=["agents:delete"]),
        AuthorizationCase(method="POST", endpoint="/api/files", required_permissions=["files:write"]),
        AuthorizationCase(method="GET", endpoint="/api/files/{fileId}", required_permissions=["files:read"]),
        AuthorizationCase(method="GET", endpoint="/api/files/{fileId}/content", required_permissions=["files:read"]),
        AuthorizationCase(method="DELETE", endpoint="/api/files/{fileId}", required_permissions=["files:delete"]),
        AuthorizationCase(method="GET", endpoint="/api/threads", required_permissions=["threads:read"]),
        AuthorizationCase(method="POST", endpoint="/api/threads", required_permissions=["threads:write"]),
        AuthorizationCase(method="DELETE", endpoint="/api/threads", required_permissions=["threads:delete"]),
        AuthorizationCase(method="GET", endpoint="/api/threads/{threadId}", required_permissions=["threads:read"]),
        AuthorizationCase(
            method="GET", endpoint="/api/threads/{threadId}/messages", required_permissions=["threads:read"]
        ),
        AuthorizationCase(
            method="POST", endpoint="/api/threads/{threadId}/runs", required_permissions=["threads:write"]
        ),
        AuthorizationCase(
            method="POST", endpoint="/api/threads/{threadId}/rename", required_permissions=["threads:write"]
        ),
        AuthorizationCase(method="DELETE", endpoint="/api/threads/{threadId}", required_permissions=["threads:delete"]),
    ],
    ids=lambda case: f"{case.method} {case.endpoint}",
)
def test_authorization_insufficient_permissions(case: AuthorizationCase):
    with make_app_client(
        config=BaseConfig.model_validate(
            {
                "security": {"authenticator": ForwardedUserAuthenticator},
                "storage": {"enabled": True},
                "agents": {"dynamic": {"enabled": True}},
            }
        )
    ) as client:
        response = client.request(
            case.method,
            case.endpoint,
            headers={"Permissions": ",".join(ALL_PERMISSIONS - set(case.required_permissions))},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "Insufficient permissions" in response.text
        for p in case.required_permissions:
            assert p in response.text
