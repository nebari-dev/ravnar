from __future__ import annotations

import contextlib
import dataclasses
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


@dataclasses.dataclass(kw_only=True)
class _ContentSecurityPolicySources:
    script: list[str] = dataclasses.field(default_factory=list)
    style: list[str] = dataclasses.field(default_factory=list)
    img: list[str] = dataclasses.field(default_factory=list)
    connect: list[str] = dataclasses.field(default_factory=list)
    font: list[str] = dataclasses.field(default_factory=list)
    worker: list[str] = dataclasses.field(default_factory=list)

    def merge(self, other: _ContentSecurityPolicySources) -> _ContentSecurityPolicySources:
        return _ContentSecurityPolicySources(
            script=[*self.script, *other.script],
            style=[*self.style, *other.style],
            img=[*self.img, *other.img],
            connect=[*self.connect, *other.connect],
            font=[*self.font, *other.font],
            worker=[*self.worker, *other.worker],
        )

    def to_csp(self) -> str:
        return "; ".join(
            [
                " ".join([id, *srcs])
                for id, srcs in [
                    # disallow everything, ...
                    ("default-src", ["'none'"]),
                    # ... except for
                    ("script-src", self.script),
                    ("style-src", self.style),
                    ("img-src", self.img),
                    ("connect-src", self.connect),
                    ("font-src", self.font),
                    ("worker-src", self.worker),
                ]
            ]
        )


CONTENT_SECURITY_POLICY_SOURCES_DEFAULT = _ContentSecurityPolicySources(
    script=["'self'"], style=["'self'"], img=["'self'", "data:"], connect=["'self'"], font=["'self'"], worker=["blob:"]
)
CONTENT_SECURITY_POLICY_DEFAULT = CONTENT_SECURITY_POLICY_SOURCES_DEFAULT.to_csp()
CONTENT_SECURITY_POLICIES = {
    # jsdelivr.net: Swagger UI bundle and CSS
    # fastapi.tiangolo.com: favicon
    "/docs": CONTENT_SECURITY_POLICY_SOURCES_DEFAULT.merge(
        _ContentSecurityPolicySources(
            script=["'unsafe-inline'", "https://cdn.jsdelivr.net"],
            style=["'unsafe-inline'", "https://cdn.jsdelivr.net"],
            img=["https://fastapi.tiangolo.com"],
        )
    ).to_csp(),
    # jsdelivr.net: ReDoc standalone bundle
    # fonts.googleapis.com, fonts.gstatic.com: Montserrat and Roboto fonts
    # cdn.redoc.ly: ReDoc logo
    # fastapi.tiangolo.com: favicon
    "/redoc": CONTENT_SECURITY_POLICY_SOURCES_DEFAULT.merge(
        _ContentSecurityPolicySources(
            script=["'unsafe-inline'", "https://cdn.jsdelivr.net"],
            style=["'unsafe-inline'", "https://cdn.jsdelivr.net", "https://fonts.googleapis.com"],
            img=["https://fastapi.tiangolo.com", "https://cdn.redoc.ly"],
            font=["https://fonts.googleapis.com", "https://fonts.gstatic.com"],
        )
    ).to_csp(),
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        response: Response = await call_next(request)

        for key, value in STATIC_SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)

        response.headers.setdefault(
            "Content-Security-Policy", CONTENT_SECURITY_POLICIES.get(request.url.path, CONTENT_SECURITY_POLICY_DEFAULT)
        )

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
