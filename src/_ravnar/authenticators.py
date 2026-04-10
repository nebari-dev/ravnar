from __future__ import annotations

import abc
import base64
from collections.abc import Awaitable, Callable
from typing import Any

import pydantic
from fastapi import Depends, Request, status
from fastapi.exceptions import HTTPException
from fastapi.security import APIKeyHeader

from _ravnar.utils import as_awaitable

from . import schema


class Authenticator(abc.ABC):
    """Authenticator base class"""

    @abc.abstractmethod
    def authenticate(self) -> schema.User: ...


class DebugAuthenticator(Authenticator):
    """Debug Authenticator"""

    async def authenticate(self, request: Request) -> schema.User:
        body = await request.body()
        try:
            body_json = await request.json()
        except Exception:
            body_json = None

        return schema.User(
            id="debug",
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

    def __init__(self, *, id_header: str = "X-Forwarded-User"):
        async def authenticate(id: str = Depends(APIKeyHeader(name=id_header))) -> schema.User:
            return schema.User(id=id)

        self.authenticate = authenticate  # type: ignore[method-assign]

    async def authenticate(self) -> schema.User:  # type: ignore[empty-body]
        # This is here to appease the ABC. The actual functionality is set in __init__
        pass


TokenValidator = Callable[[str], schema.User] | Callable[[str], Awaitable[schema.User]]


class OIDCConfig(pydantic.BaseModel):
    jwks_uri: pydantic.HttpUrl
    id_token_signing_alg_values_supported: list[str]


class OIDCUser(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="allow")

    sub: str


class OIDCTokenValidator:
    """OIDC Token Validator"""

    def __init__(self, *, issuer: str, algorithms: list[str] | None = None, audience: str | None = None):
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

    def __call__(self, token: str) -> schema.User:
        import jwt

        try:
            payload = jwt.decode(token, self._jwks_client.get_signing_key_from_jwt(token).key, **self._decode_kwargs)
        except jwt.ExpiredSignatureError:
            raise
        except jwt.InvalidTokenError:
            raise

        try:
            oidc_user = OIDCUser.model_validate(payload)
        except pydantic.ValidationError:
            raise

        return schema.User(id=oidc_user.sub, data=oidc_user.model_dump(exclude={"sub"}))


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

    async def authenticate(self, token: str = Depends(get_bearer_token)) -> schema.User:
        return await as_awaitable(self._token_validator, token)
