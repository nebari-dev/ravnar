from __future__ import annotations

import abc
import base64
from collections.abc import Awaitable, Callable
from typing import Any

import pydantic
from fastapi import Depends, Request, status
from fastapi.exceptions import HTTPException
from fastapi.security import APIKeyHeader
from opentelemetry import trace

from _ravnar.auth import ALL_PERMISSIONS, Permission, User
from _ravnar.observability import traced
from _ravnar.utils import as_awaitable


class Authenticator(abc.ABC):
    """Authenticator base class"""

    @abc.abstractmethod
    def authenticate(self) -> User: ...


class DebugAuthenticator(Authenticator):
    """Debug Authenticator"""

    @traced
    async def authenticate(self, request: Request) -> User:
        body = await request.body()
        try:
            body_json = await request.json()
        except Exception:
            body_json = None

        return User(
            id="debug",
            permissions=list(ALL_PERMISSIONS),
            data={
                "method": request.method,
                "headers": dict(request.headers),
                "query_params": dict(request.query_params),
                "cookies": request.cookies,
                "body_b64": base64.b64encode(body).decode(),
                "body_json": body_json,
            },
        )


class ForwardedUserAuthenticator(Authenticator):
    """Forwarded User Authenticator"""

    def __init__(
        self,
        *,
        id_header: str = "X-Forwarded-User",
        permissions_header: str = "X-Forwarded-Permissions",
    ):
        @traced(name="ForwardedUserAuthenticator.authenticate")
        async def authenticate(
            id: str = Depends(APIKeyHeader(name=id_header)),
            permissions: str | None = Depends(APIKeyHeader(name=permissions_header, auto_error=False)),
        ) -> User:
            # FIXME: validation with APIheader?
            perm_list = [p.strip() for p in permissions.split(",") if p.strip()] if permissions else []
            return User(id=id, permissions=perm_list)

        self.authenticate = authenticate  # type: ignore[method-assign]

    async def authenticate(self) -> User:  # type: ignore[empty-body]
        # This is here to appease the ABC. The actual functionality is set in __init__
        pass


TokenValidator = Callable[[str], User] | Callable[[str], Awaitable[User]]


class OIDCConfig(pydantic.BaseModel):
    jwks_uri: pydantic.HttpUrl
    id_token_signing_alg_values_supported: list[str]


class OIDCUser(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="allow")

    sub: str


class OIDCTokenValidator:
    """OIDC Token Validator"""

    def __init__(
        self,
        *,
        issuer: str,
        algorithms: list[str] | None = None,
        audience: str | None = None,
        permissions_claim: str | None = None,
        default_permissions: list[str] | None = None,
    ):
        import httpx
        import jwt.types

        response = httpx.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration").raise_for_status()
        oidc_config = OIDCConfig.model_validate_json(response.content)

        self._jwks_client = jwt.PyJWKClient(str(oidc_config.jwks_uri))

        decode_kwargs: dict[str, Any] = {}
        decode_options: jwt.types.Options = {}
        decode_kwargs["options"] = decode_options

        decode_kwargs["issuer"] = issuer

        if algorithms is None:
            # only allow asymmetric algorithms by default
            algorithms = [
                a for a in oidc_config.id_token_signing_alg_values_supported if a.startswith(("RS", "ES", "PS"))
            ]
        decode_kwargs["algorithms"] = algorithms

        if audience:
            decode_kwargs["audience"] = audience
        else:
            decode_options["verify_aud"] = False

        self._decode_kwargs = decode_kwargs
        self._permissions_claim = permissions_claim

        if default_permissions is None:
            default_permissions = []
        else:
            pydantic.TypeAdapter(list[Permission]).validate_python(default_permissions)
        self._default_permissions = default_permissions

    @traced(name="OIDCTokenValidator")
    def __call__(self, token: str) -> User:
        try:
            return self._validate(token)
        except HTTPException as exc:
            span = trace.get_current_span()
            span.add_event("validation_failure", attributes={"reason": exc.detail})
            raise

    def _validate(self, token: str) -> User:
        import jwt

        try:
            payload = jwt.decode(token, self._jwks_client.get_signing_key_from_jwt(token).key, **self._decode_kwargs)
        except jwt.ExpiredSignatureError as exc:
            raise HTTPException(detail="JWT expired", status_code=status.HTTP_401_UNAUTHORIZED) from exc
        except jwt.InvalidTokenError as exc:
            raise HTTPException(detail="JWT invalid", status_code=status.HTTP_401_UNAUTHORIZED) from exc

        try:
            oidc_user = OIDCUser.model_validate(payload)
        except pydantic.ValidationError as exc:
            raise HTTPException(detail="JWT payload invalid", status_code=status.HTTP_401_UNAUTHORIZED) from exc

        if self._permissions_claim is not None:
            claim_value = payload.get(self._permissions_claim)
            if claim_value is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Required permissions claim missing in token",
                )
            if not isinstance(claim_value, list) or not all(isinstance(p, str) for p in claim_value):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Permissions claim must be a list of strings",
                )
            permissions = claim_value
        else:
            permissions = self._default_permissions

        return User(id=oidc_user.sub, permissions=permissions, data=oidc_user.model_dump(exclude={"sub"}))


async def get_bearer_token(
    authorization: str | None = Depends(APIKeyHeader(name="Authorization", auto_error=False)),
) -> str:
    if authorization is None:
        raise HTTPException(
            detail="Authorization header required",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    match authorization.split():
        case [scheme, token]:
            if scheme.lower() != "bearer":
                raise HTTPException(
                    detail="Bearer scheme required",
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )
        case _:
            raise HTTPException(
                detail="Bearer authorization required",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

    return token


class BearerTokenAuthenticator(Authenticator):
    """Bearer Token Authenticator"""

    def __init__(self, token_validator: TokenValidator) -> None:
        self._token_validator = token_validator

    @traced
    async def authenticate(self, token: str = Depends(get_bearer_token)) -> User:
        return await as_awaitable(self._token_validator, token)
