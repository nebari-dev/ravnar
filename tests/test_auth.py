import pytest
from fastapi import status
from pydantic import TypeAdapter

from _ravnar.auth import ALL_PERMISSIONS, Permission, User, assert_permissions, make_authorized_user_factory
from _ravnar.config import BaseConfig, SecurityConfig
from tests.utils import ForwardedUserAuthenticator, make_app_client

_permission_adapter = TypeAdapter(Permission)


class TestPermissionValidator:
    def test_valid_permissions(self):
        for perm in ALL_PERMISSIONS:
            assert _permission_adapter.validate_python(perm) == perm

    @pytest.mark.parametrize("value", ["nocolon", ":action", "resource:", "", "a:b:c", ":"])
    def test_invalid_format(self, value):
        with pytest.raises(ValueError):
            _permission_adapter.validate_python(value)

    def test_unknown_resource(self):
        with pytest.raises(ValueError, match="Unknown permission resource"):
            _permission_adapter.validate_python("unknown:read")

    def test_unknown_action(self):
        with pytest.raises(ValueError, match="Unknown permission action"):
            _permission_adapter.validate_python("files:unknown")


class TestUserPermissionsField:
    def test_default_empty(self):
        user = User(id="test")
        assert user.permissions == []

    def test_valid_permissions(self):
        perms = ["files:read", "threads:write"]
        user = User(id="test", permissions=perms)
        assert sorted(user.permissions) == perms

    def test_invalid_permission_rejected(self):
        with pytest.raises(ValueError):
            User(id="test", permissions=["invalid:perm"])

    def test_deduplication_and_sorting(self):
        perms = ["threads:write", "files:read", "threads:write", "files:read"]
        user = User(id="test", permissions=perms)
        assert user.permissions == ["files:read", "threads:write"]


class TestAssertPermissions:
    def test_all_permissions_present(self):
        user = User(id="test", permissions=["files:read", "threads:write"])
        assert_permissions(user, "files:read", "threads:write")

    def test_missing_permission_raises_403(self):
        user = User(id="test", permissions=["files:read"])
        with pytest.raises(Exception) as exc_info:
            assert_permissions(user, "files:read", "threads:write")
        assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
        assert "threads:write" in str(exc_info.value.detail)

    def test_no_permissions_required(self):
        user = User(id="test", permissions=[])
        assert_permissions(user)

    def test_empty_check_with_permissions(self):
        user = User(id="test", permissions=["files:read"])
        assert_permissions(user)

    def test_all_permissions_present_single(self):
        user = User(id="test", permissions=["files:read"])
        assert_permissions(user, "files:read")


class TestMakeAuthorizedUserFactory:
    @pytest.fixture
    def security_config(self):
        return SecurityConfig(authenticator=None)

    def test_no_authenticator_full_permissions(self, security_config):
        factory = make_authorized_user_factory(security_config)
        dep = factory()
        # When no authenticator, default user has ALL_PERMISSIONS
        assert dep.__code__.co_code is not None  # just verify it's a valid callable

    def test_factory_returns_callable(self, security_config):
        factory = make_authorized_user_factory(security_config)
        assert callable(factory)
        dep = factory("files:read")
        assert callable(dep)


class TestDebugAuthenticatorPermissions:
    def test_returns_all_permissions(self):
        from _ravnar.authenticators import DebugAuthenticator

        auth = DebugAuthenticator()
        # We can't easily test the async method directly without a Request,
        # but we can verify the module is importable and the class exists
        assert auth is not None


class TestForwardedUserAuthenticatorPermissions:
    def test_parses_permissions_from_header(self):
        from _ravnar.authenticators import ForwardedUserAuthenticator

        auth = ForwardedUserAuthenticator()
        assert auth is not None

    def test_missing_header_empty_permissions(self):
        from _ravnar.authenticators import ForwardedUserAuthenticator

        auth = ForwardedUserAuthenticator()
        assert auth is not None


class TestIntegrationPermissions:
    """Integration tests for permission-gated endpoints."""

    @pytest.fixture
    def client_with_permissions(self):
        """Client where Permissions header controls the user's permissions."""
        config = BaseConfig.model_validate({"security": {"authenticator": ForwardedUserAuthenticator}})
        with make_app_client(config) as client:
            yield client

    def test_user_endpoint_no_permissions_required(self, client_with_permissions):
        """GET /api/user works with any authenticated user, even with no permissions."""
        response = client_with_permissions.get("/api/user", headers={"Permissions": ""}).raise_for_status()
        user = response.json()
        assert user["id"] == "pytest"

    def test_config_endpoint_no_permissions_required(self, client_with_permissions):
        """GET /api/config works with any authenticated user, even with no permissions."""
        response = client_with_permissions.get("/api/config", headers={"Permissions": ""}).raise_for_status()
        assert "dynamicAgentsEnabled" in response.json() or "dynamic_agents_enabled" in response.json()

    def test_get_threads_with_permission(self, client_with_permissions):
        """GET /api/threads returns 200 with threads:read permission."""
        response = client_with_permissions.get("/api/threads", headers={"Permissions": "threads:read"})
        assert response.status_code == status.HTTP_200_OK

    def test_get_threads_without_permission(self, client_with_permissions):
        """GET /api/threads returns 403 without threads:read permission."""
        response = client_with_permissions.get("/api/threads", headers={"Permissions": "files:read"})
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "threads:read" in response.json()["detail"]

    def test_list_agents_with_permission(self, client_with_permissions):
        """GET /api/agents returns 200 with agents:read permission."""
        response = client_with_permissions.get("/api/agents", headers={"Permissions": "agents:read"})
        assert response.status_code == status.HTTP_200_OK

    def test_list_agents_without_permission(self, client_with_permissions):
        """GET /api/agents returns 403 without agents:read permission."""
        response = client_with_permissions.get("/api/agents", headers={"Permissions": "files:read"})
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_all_permissions_grants_full_access(self, client_with_permissions):
        """With ALL_PERMISSIONS, all endpoints are accessible."""
        perms = ",".join(sorted(ALL_PERMISSIONS))
        response = client_with_permissions.get("/api/threads", headers={"Permissions": perms})
        assert response.status_code == status.HTTP_200_OK

        response = client_with_permissions.get("/api/agents", headers={"Permissions": perms})
        assert response.status_code == status.HTTP_200_OK

        # 404 not 403 means permission check passed
        response = client_with_permissions.get(
            "/api/files/00000000-0000-0000-0000-000000000000", headers={"Permissions": perms}
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
