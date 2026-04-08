__all__ = ["APIRouter", "BaseModel", "Page", "Pagination", "TModel", "User", "UtcDateTime", "create_str_literal"]

import contextlib
import functools
import getpass
import os
from datetime import UTC, datetime
from typing import Annotated, Any, Generic, Literal, Self, TypeVar, cast

import fastsse
from fastapi.routing import APIRoute
from pydantic import AfterValidator, BeforeValidator, ConfigDict, Field, ValidationInfo, WithJsonSchema, field_validator
from pydantic import BaseModel as _BaseModel
from pydantic.alias_generators import to_camel, to_snake
from pydantic_core import PydanticUndefined


class ExcludeNoneAPIRoute(APIRoute):
    def __init__(self, *args: Any, **kwargs: Any):
        kwargs["response_model_exclude_none"] = True
        super().__init__(*args, **kwargs)


class APIRouter(fastsse.APIRouter):
    def __init__(self, *args: Any, route_class: type[APIRoute] = ExcludeNoneAPIRoute, **kwargs: Any) -> None:
        super().__init__(*args, route_class=route_class, **kwargs)


class BaseModel(_BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


TModel = TypeVar("TModel", bound=BaseModel)


# TODO: if we can depend on Python >= 3.11 instead of wrapping a Literal we can create it here.
#  By then Literal supports variadic input so we can take a list of values and do Literal[*values]
def create_str_literal(*values: str, default: str | None = None) -> Any:
    if default is None:
        default = PydanticUndefined  # type: ignore[assignment]
    else:
        assert default in values
        default = to_camel(default)

    return Annotated[
        Literal[*values],
        BeforeValidator(lambda v: to_snake(v) if isinstance(v, str) else v),
        WithJsonSchema({"type": "string", "enum": [to_camel(v) for v in values]}),
        Field(default=default),
    ]


def _set_utc_timezone(v: datetime) -> datetime:
    if v.tzinfo is None:
        return v.replace(tzinfo=UTC)

    return v.astimezone(UTC)


UtcDateTime = Annotated[datetime, AfterValidator(_set_utc_timezone)]


class User(BaseModel):
    id: str
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def default(cls) -> Self:
        return cls(id=cls._current_user())

    @staticmethod
    @functools.cache
    def _current_user() -> str:
        with contextlib.suppress(Exception):
            return getpass.getuser()
        with contextlib.suppress(Exception):
            return os.getlogin()
        return "Huginn"


TSortBy = TypeVar("TSortBy", bound=str)
SortOrder = Literal["ascending", "descending"]


class Pagination(BaseModel, Generic[TSortBy]):
    page_size: int = Field(default=10)
    page_number: int = Field(default=1, ge=1)
    sort_by: TSortBy | None = None
    sort_order: SortOrder = "ascending"

    @field_validator("page_size", mode="after")
    @classmethod
    def _validate_page_size(cls, page_size: int) -> int:
        if not (page_size == -1 or page_size > 0):
            raise ValueError("pageSize can either be -1 or > 0")

        return page_size

    @classmethod
    def _is_single_page(cls, page_size: int) -> bool:
        return page_size == -1

    @field_validator("page_number", mode="after")
    @classmethod
    def _validate_page_number(cls, page_number: int, info: ValidationInfo) -> int:
        page_size = cast(int | None, info.data.get("page_size"))
        if page_size is not None and cls._is_single_page(page_size) and page_number != 1:
            raise ValueError("only page number 1 is allowed for infinite page size")

        return page_number

    @functools.cached_property
    def is_single_page(self) -> bool:
        return self._is_single_page(self.page_size)

    @classmethod
    def as_single_page(cls, sort_by: TSortBy | None = None, sort_order: SortOrder = "ascending") -> Self:
        return cls(page_size=-1, page_number=1, sort_by=sort_by, sort_order=sort_order)


class Page(BaseModel, Generic[TModel]):
    page_size: int
    page_number: int
    total_count: int
    page_count: int
    items: list[TModel]


class ServerSentEvent(BaseModel, Generic[TModel]):
    event: str = "message"
    data: TModel
    id: str | None = None
    retry: int | None = None
