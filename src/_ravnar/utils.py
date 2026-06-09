from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import json
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar, Generic, TypeVar, cast, get_type_hints

import jinja2
import jinja2.sandbox
from pydantic import (
    BaseModel,
    Field,
    ImportString,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core import PydanticCustomError
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool
from typing_extensions import ParamSpec

T = TypeVar("T")
P = ParamSpec("P")


def as_awaitable(fn: Callable[P, T] | Callable[P, Awaitable[T]], *args: P.args, **kwargs: P.kwargs) -> Awaitable[T]:
    if inspect.iscoroutinefunction(fn):
        fn = cast(Callable[..., Awaitable[T]], fn)
        awaitable = fn(*args, **kwargs)
    else:
        fn = cast(Callable[..., T], fn)
        awaitable = run_in_threadpool(fn, *args, **kwargs)

    return awaitable


def as_async_iterator(
    fn: Callable[..., Iterator[T]] | Callable[..., AsyncIterator[T]], *args: Any, **kwargs: Any
) -> AsyncIterator[T]:
    if inspect.isasyncgenfunction(fn):
        fn = cast(Callable[..., AsyncIterator[T]], fn)
        async_iterator = fn(*args, **kwargs)
    else:
        fn = cast(Callable[..., Iterator[T]], fn)
        async_iterator = iterate_in_threadpool(fn(*args, **kwargs))

    return async_iterator


class _AsyncContextManagerWrapper(contextlib.AbstractAsyncContextManager[T]):
    def __init__(self, cm: contextlib.AbstractContextManager[T]) -> None:
        self._cm = cm

    async def __aenter__(self) -> T:
        return await run_in_threadpool(self._cm.__enter__)

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: Any
    ) -> bool | None:
        return await run_in_threadpool(self._cm.__exit__, exc_type, exc_val, exc_tb)


def as_async_context_manager(
    cm: contextlib.AbstractContextManager[T] | contextlib.AbstractAsyncContextManager[T],
) -> contextlib.AbstractAsyncContextManager[T]:
    if isinstance(cm, contextlib.AbstractAsyncContextManager):
        return cm
    return _AsyncContextManagerWrapper(cm)


def kebabize(s: str) -> str:
    return re.sub(r"(([a-z])(?=[A-Z])|([A-Z])(?=[A-Z][a-z]))", r"\1-", s).lower()


def resolve_forward_references(c: Callable[..., T] | Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    annotations = get_type_hints(c, include_extras=True)
    signature = (s := inspect.signature(c)).replace(
        parameters=[
            p.replace(annotation=annotations[p.name] if p.annotation is not inspect.Parameter.empty else p.annotation)
            for p in s.parameters.values()
        ],
        return_annotation=annotations["return"]
        if s.return_annotation is not inspect.Signature.empty
        else s.return_annotation,
    )

    # This wrapper is required, because we cannot update the signature on some callable types directly. The wrapper must
    # handle the sync / async nature of the wrapped function manually, because the metadata is lost, which FastAPI
    # requires to do so automatically.
    @functools.wraps(c)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        return await as_awaitable(c, *args, **kwargs)

    wrapper.__annotations__ = annotations
    wrapper.__signature__ = signature  # type: ignore[attr-defined]

    return wrapper


def now() -> datetime:
    return datetime.now(tz=UTC)


class TemplateRenderError(ValueError):
    def __init__(self, *, template: str, reason: str, message: str) -> None:
        self.template = template
        self.reason = reason
        self.message = message
        super().__init__(message)


def render_template(s: Any, context: dict[str, str]) -> Any:
    if isinstance(s, str):
        env = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined)
        return env.from_string(s).render(**context)
    if isinstance(s, dict):
        return {render_template(k, context): render_template(v, context) for k, v in s.items()}
    if isinstance(s, list):
        return [render_template(v, context) for v in s]
    return s


class ImportStringWithParams(BaseModel, Generic[T]):
    _render_template_context: ClassVar[contextvars.ContextVar[dict[str, str] | None]] = contextvars.ContextVar(
        "render_template_context", default=None
    )

    @classmethod
    def explicit_render_template_context(cls, ctx: dict[str, str]) -> contextlib.AbstractContextManager[dict[str, str]]:
        @contextlib.contextmanager
        def cm() -> Iterator[dict[str, str]]:
            cls._render_template_context.set(ctx)
            yield ctx
            cls._render_template_context.set(None)

        return cm()

    cls_or_fn: ImportString[type[T] | Callable[..., T]]
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _from_str_or_type_or_callable(cls, m: Any) -> Any:
        if isinstance(m, str):
            with contextlib.suppress(json.JSONDecodeError):
                m = json.loads(m)

        if isinstance(m, (str, type)) or callable(m):
            m = {"cls_or_fn": m}
        return m

    @model_validator(mode="before")
    @classmethod
    def _validate_nested(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "params" not in data or not isinstance(data["params"], dict):
            return data

        def validate(v: Any, loc: tuple[str | int, ...]) -> Any:
            match v:
                case dict():
                    if "cls_or_fn" in v:
                        try:
                            return cls.model_validate(v)
                        except ValidationError as ve:
                            # rewrite the errors to include the proper location
                            raise ValidationError.from_exception_data(
                                ve.title,
                                [
                                    {
                                        "type": PydanticCustomError(e["type"], e["msg"], e.get("ctx")),
                                        "loc": (*loc, *e["loc"]),
                                        "input": e["input"],
                                    }
                                    for e in ve.errors()
                                ],
                            ) from None

                    return {k: validate(v, (*loc, k)) for k, v in v.items()}
                case list():
                    return [validate(v, (*loc, i)) for i, v in enumerate(v)]
                case _:
                    return v

        data["params"] = {k: validate(v, ("params", k)) for k, v in data["params"].items()}

        return data

    @field_validator("cls_or_fn", "params", mode="before")
    @classmethod
    def _render_field_templates(cls, f: Any) -> Any:
        if isinstance(f, str):
            return cls._render_template(f)

        return f

    @field_validator("params", mode="after")
    @classmethod
    def _render_param_items(cls, params: dict[str, Any]) -> dict[str, Any]:
        return {cls._render_template(k): cls._render_template(v) for k, v in params.items()}

    @classmethod
    def _render_template(cls, s: Any) -> Any:
        explicit_ctx = cls._render_template_context.get()
        ctx = explicit_ctx if explicit_ctx is not None else dict(os.environ)
        try:
            return render_template(s, ctx)
        except (jinja2.exceptions.SecurityError, jinja2.exceptions.UndefinedError) as exc:
            if explicit_ctx is not None:
                raise TemplateRenderError(
                    template=str(exc),
                    reason=type(exc).__name__,
                    message="Invalid configuration",
                ) from exc
            raise

    @model_serializer(mode="wrap")
    def _serialize(self, nxt: SerializerFunctionWrapHandler) -> Any:
        s = nxt(self)
        if not self.params:
            s = s["cls_or_fn"]
        return s

    def __call__(self) -> T:
        def call(v: Any) -> Any:
            match v:
                case ImportStringWithParams():
                    return v()
                case dict():
                    return {k: call(v) for k, v in v.items()}
                case list():
                    return [call(x) for x in v]
                case _:
                    return v

        return self.cls_or_fn(**{k: call(v) for k, v in self.params.items()})
