from __future__ import annotations

import contextlib
import functools
import getpass
import os
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Self

from fastapi import Depends, HTTPException, Request, Response, status
from pydantic import AfterValidator, Field, field_validator
from pydantic import BaseModel as _BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from _ravnar.utils import resolve_forward_references

CallNext = Callable[[Request], Awaitable[Response]]

STATIC_SECURITY_HEADERS = {
    # Prevent MIME-type sniffing attacks (browsers won't guess Content-Type from file contents)
    "X-Content-Type-Options": "nosniff",
    # Prevent clickjacking by disallowing the page from being embedded in frames/iframes
    "X-Frame-Options": "DENY",
}

# script, style, img
CSPSources = tuple[list[str], list[str], list[str]]


def make_csp(*sources: CSPSources) -> str:
    script_src, style_src, img_src = zip(*sources, strict=True)
    return ";".join(
        [
            " ".join([id, *s])
            for id, s in [
                # disallow everything, ...
                ("default-src", "'none'"),
                # ... except for
                ("script-src", script_src),
                ("style-src", style_src),
                ("img-src", img_src),
            ]
        ]
    )


CSP_SOURCES_DEFAULT: CSPSources = (["'self'"], ["'self'"], ["'self'", "data:"])
CSP_DEFAULT = make_csp(CSP_SOURCES_DEFAULT)
CSP = {
    "/docs": make_csp(
        CSP_SOURCES_DEFAULT,
        # jsdelivr.net: Swagger UI bundle and CSS
        # fastapi.tiangolo.com: favicon
        (
            ["'unsafe-inline'", "https://cdn.jsdelivr.net"],
            ["'unsafe-inline'", "https://cdn.jsdelivr.net"],
            ["https://fastapi.tiangolo.com"],
        ),
    ),
    "/redoc": make_csp(
        CSP_SOURCES_DEFAULT,
        # jsdelivr.net: ReDoc standalone bundle
        # fonts.googleapis.com: Montserrat and Roboto fonts
        # fastapi.tiangolo.com: favicon
        (
            ["'unsafe-inline'", "https://cdn.jsdelivr.net"],
            ["'unsafe-inline'", "https://cdn.jsdelivr.net", "https://fonts.googleapis.com"],
            ["https://fastapi.tiangolo.com"],
        ),
    ),
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        response: Response = await call_next(request)

        for key, value in STATIC_SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)

        response.headers.setdefault("Content-Security-Policy", CSP.get(request.url.path, CSP_DEFAULT))

        return response


PERMISSION_REGISTRY: dict[str, list[str]] = {
    "files": ["read", "write", "delete"],
    "threads": ["read", "write", "delete"],
    "agents": ["read", "write", "delete"],
}

ALL_PERMISSIONS: frozenset[str] = frozenset(
    f"{resource}:{action}" for resource, actions in PERMISSION_REGISTRY.items() for action in actions
)


def _validate_permission(value: str) -> str:
    match = re.match(r"^(?P<resource>[^:]+):(?P<action>[^:]+)$", value)
    if match is None:
        raise ValueError(f"Invalid permission format: {value!r}. Expected <resource>:<action>")
    resource = match.group("resource")
    action = match.group("action")
    if resource not in PERMISSION_REGISTRY:
        raise ValueError(f"Unknown permission resource: {resource!r}")
    if action not in PERMISSION_REGISTRY[resource]:
        raise ValueError(f"Unknown permission action: {action!r} for resource {resource!r}")
    return value


Permission = Annotated[str, AfterValidator(_validate_permission)]


class User(_BaseModel):
    """An authenticated user: their identity, arbitrary data, and permissions."""

    id: str
    data: dict[str, Any] = Field(default_factory=dict)
    permissions: list[Permission] = Field(default_factory=list)

    @field_validator("permissions", mode="before")
    @classmethod
    def _validate_permissions(cls, v: list[str]) -> list[str]:
        return sorted(set(v))

    @classmethod
    def default(cls) -> Self:
        return cls(id=cls._current_user(), permissions=list(ALL_PERMISSIONS))

    @staticmethod
    @functools.cache
    def _current_user() -> str:
        with contextlib.suppress(Exception):
            return getpass.getuser()
        with contextlib.suppress(Exception):
            return os.getlogin()
        return "Huginn"


def assert_permissions(user: User, *permissions: str) -> None:
    """Raise HTTPException(403) if user lacks any required permission."""
    if missing := set(permissions) - set(user.permissions):
        missing_permissions = ",".join(sorted(missing))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Insufficient permissions. Missing: {missing_permissions}",
        )


def make_authorized_user_factory(
    security_config: Any,
) -> Callable[..., Any]:
    authenticated_user: Callable[..., Awaitable[User]]
    if security_config.authenticator is None:

        async def authenticated_user() -> User:
            return User.default()
    else:
        authenticator = security_config.authenticator()
        authenticated_user = resolve_forward_references(authenticator.authenticate)

    def authorized_user_with(*permissions: str) -> Callable[..., Awaitable[User]]:
        async def authorized_user(
            user: User = Depends(authenticated_user),  # noqa:B008
        ) -> User:
            assert_permissions(user, *permissions)
            return user

        return authorized_user

    return authorized_user_with
