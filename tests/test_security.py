import pydantic
import pytest
from fastapi import status
from fastapi.responses import Response
from starlette.testclient import TestClient

from _ravnar.security import (
    ALL_PERMISSIONS,
    CONTENT_SECURITY_POLICIES,
    CONTENT_SECURITY_POLICY_DEFAULT,
    STATIC_SECURITY_HEADERS,
    Permission,
    User,
    assert_permissions,
)


class TestPermissionValidator:
    @pytest.mark.parametrize("permission", ALL_PERMISSIONS)
    def test_valid_permissions(self, permission):
        assert pydantic.TypeAdapter(Permission).validate_python(permission) == permission

    @pytest.mark.parametrize("value", ["nocolon", ":action", "resource:", "", "a:b:c", ":"])
    def test_invalid_format(self, value):
        with pytest.raises(ValueError):
            pydantic.TypeAdapter(Permission).validate_python(value)

    def test_unknown_resource(self):
        with pytest.raises(ValueError, match="Unknown permission resource"):
            pydantic.TypeAdapter(Permission).validate_python("unknown:read")

    def test_unknown_action(self):
        with pytest.raises(ValueError, match="Unknown permission action"):
            pydantic.TypeAdapter(Permission).validate_python("files:unknown")


class TestUserPermissionsField:
    def test_default_empty(self):
        user = User(id="test")
        assert user.permissions == []

    def test_valid_permissions(self):
        permissions = ["files:read", "threads:write"]
        user = User(id="test", permissions=permissions)
        assert sorted(user.permissions) == permissions

    def test_invalid_permission_rejected(self):
        with pytest.raises(ValueError):
            User(id="test", permissions=["invalid:perm"])

    def test_deduplication_and_sorting(self):
        permissions = ["threads:write", "files:read", "threads:write", "files:read"]
        user = User(id="test", permissions=permissions)
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


class TestStaticSecurityHeaders:
    @pytest.mark.parametrize(
        "endpoint",
        ["/health", "/version", "/nonexistent", "/api/user"],
    )
    def test_security_headers_present(self, app_client: TestClient, endpoint: str):
        response = app_client.get(endpoint)

        for header_name, expected_value in STATIC_SECURITY_HEADERS.items():
            assert header_name in response.headers
            assert response.headers[header_name] == expected_value

    @pytest.mark.parametrize("header", STATIC_SECURITY_HEADERS)
    def test_headers_not_overwritten_when_already_set(self, app_client: TestClient, header):
        value = "sentinel"

        @app_client.app.get("/custom-header")
        def custom_header_endpoint():
            return Response("", headers={header: value})

        response = app_client.get("/custom-header")
        assert response.headers[header] == value


class TestContentSecurityPolicyHeader:
    @pytest.mark.parametrize(
        "endpoint",
        ["/health", "/version", "/nonexistent", "/api/user"],
    )
    def test_default_csp(self, app_client: TestClient, endpoint):
        response = app_client.get(endpoint)
        assert "Content-Security-Policy" in response.headers
        assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY_DEFAULT

    @pytest.mark.parametrize(("endpoint", "csp"), list(CONTENT_SECURITY_POLICIES.items()))
    def test_custom_csp(self, app_client: TestClient, endpoint, csp):
        response = app_client.get(endpoint)
        assert "Content-Security-Policy" in response.headers
        assert response.headers["Content-Security-Policy"] == csp

    def test_csp_not_overwritten_when_already_set(self, app_client: TestClient):
        value = "sentinel"

        @app_client.app.get("/custom-csp")
        def custom_csp_endpoint():
            return Response("", headers={"Content-Security-Policy": value})

        response = app_client.get("/custom-csp")
        assert response.headers["Content-Security-Policy"] == value

    @pytest.mark.parametrize("csp", [CONTENT_SECURITY_POLICY_DEFAULT, *CONTENT_SECURITY_POLICIES.values()])
    def test_csp_format_is_valid(self, csp):
        srcs = dict(src.strip().split(" ", 1) for src in csp.split(";"))
        assert set(srcs.keys()) == {"default-src", "script-src", "style-src", "img-src", "connect-src"}
