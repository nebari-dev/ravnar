from __future__ import annotations

import abc
import base64
from collections.abc import Awaitable, Callable
from typing import Any

import pydantic
import structlog
from fastapi import Depends, Request, status
from fastapi.exceptions import HTTPException
from fastapi.security import APIKeyHeader
from opentelemetry import trace

from _ravnar.observability import traced
from _ravnar.utils import as_awaitable

from . import schema


class Authenticator(abc.ABC):
    """Authenticator base class"""

    @abc.abstractmethod
    def authenticate(self) -> schema.User: ...


class DebugAuthenticator(Authenticator):
    """Debug Authenticator"""

    @traced
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
        @traced
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
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("OIDCTokenValidator.validate"):
            import jwt

            logger = structlog.get_logger()
            try:
                payload = jwt.decode(
                    token, self._jwks_client.get_signing_key_from_jwt(token).key, **self._decode_kwargs
                )
            except jwt.ExpiredSignatureError as exc:
                span = trace.get_current_span()
                span.add_event("auth_failure", attributes={"reason": "JWT expired"})
                logger.warning("authentication failed", reason="JWT expired")
                raise HTTPException(detail="JWT expired", status_code=status.HTTP_401_UNAUTHORIZED) from exc
            except jwt.InvalidTokenError as exc:
                span = trace.get_current_span()
                span.add_event("auth_failure", attributes={"reason": "JWT invalid"})
                logger.warning("authentication failed", reason="JWT invalid")
                raise HTTPException(detail="JWT invalid", status_code=status.HTTP_401_UNAUTHORIZED) from exc

            try:
                oidc_user = OIDCUser.model_validate(payload)
            except pydantic.ValidationError as exc:
                span = trace.get_current_span()
                span.add_event("auth_failure", attributes={"reason": "JWT payload invalid"})
                logger.warning("authentication failed", reason="JWT payload invalid")
                raise HTTPException(detail="JWT payload invalid", status_code=status.HTTP_401_UNAUTHORIZED) from exc

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

    @traced
    async def authenticate(self, token: str = Depends(get_bearer_token)) -> schema.User:
        return await as_awaitable(self._token_validator, token)
