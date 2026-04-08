from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Generic, Self, TypeVar

import jinja2
import l2sl
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
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)
from upath import UPath

from .agents import Agent, DefaultAgent
from .authenticators import Authenticator

T = TypeVar("T")


def interactive_session() -> bool:
    return sys.stdout.isatty()


def render_template(s: Any) -> Any:
    if isinstance(s, str):
        return jinja2.Environment().from_string(s).render(**os.environ)
    if isinstance(s, dict):
        return {render_template(k): render_template(v) for k, v in s.items()}
    if isinstance(s, list):
        return [render_template(v) for v in s]
    return s


class ImportStringWithParams(BaseModel, Generic[T]):
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

    @field_validator("cls_or_fn", "params", mode="before")
    @classmethod
    def _render_field_templates(cls, f: Any) -> Any:
        if isinstance(f, str):
            return render_template(f)

        return f

    @field_validator("params", mode="after")
    @classmethod
    def _render_param_items(cls, params: dict[str, Any]) -> dict[str, Any]:
        return {render_template(k): render_template(v) for k, v in params.items()}

    @model_serializer(mode="wrap")
    def _serialize(self, nxt: SerializerFunctionWrapHandler) -> Any:
        s = nxt(self)
        if not self.params:
            s = s["cls_or_fn"]
        return s

    def __call__(self) -> T:
        def call(v: Any) -> Any:
            match v:
                case dict():
                    try:
                        return ImportStringWithParams.model_validate(v).__call__()
                    except ValidationError:
                        return {k: call(v) for k, v in v.items()}
                case list():
                    return [call(x) for x in v]
                case _:
                    return v

        return self.cls_or_fn(**{k: call(v) for k, v in self.params.items()})


class RenderableMixin:
    @field_validator("*", mode="before")
    @classmethod
    def _render_templates(cls, data: Any) -> Any:
        return render_template(data)


class LoggingConfig(BaseModel, RenderableMixin):
    level: l2sl.LogLevel = l2sl.LogLevel("info")
    as_json: bool = Field(default_factory=lambda: not interactive_session())


class TracingConfig(BaseModel, RenderableMixin):
    endpoint: str | None = None
    as_logs: bool = Field(default_factory=lambda values: interactive_session() and values["endpoint"] is None)


class ServerConfig(BaseModel, RenderableMixin):
    hostname: str = "127.0.0.1"
    port: int = 8000
    proxy_headers: bool = False
    forwarded_allow_ips: list[str] = Field(default_factory=lambda: ["*"])
    root_path: str = ""
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    tracing: TracingConfig = Field(default_factory=TracingConfig)


class CORSConfig(BaseModel, RenderableMixin):
    allowed_origins: list[str] = Field(default_factory=lambda: ["*"])
    allowed_headers: list[str] = Field(default_factory=list)


class Security(BaseModel, RenderableMixin):
    authenticator: ImportStringWithParams[Authenticator] | None = None
    cors: CORSConfig = Field(default_factory=CORSConfig)


def _local_storage() -> Path:
    if (ls := os.environ.get("RAVNAR_LOCAL_STORAGE")) is None:
        p = Path.cwd() / ".ravnar_local"
    else:
        p = Path(ls).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


class StorageConfig(BaseModel, RenderableMixin):
    database_dsn: str = Field(default_factory=lambda: f"sqlite:///{_local_storage() / 'state.db'}")
    file_storage_path: UPath = Field(default_factory=lambda: UPath(_local_storage() / "files"))


class BaseConfig(BaseSettings, RenderableMixin):
    server: ServerConfig = Field(default_factory=ServerConfig)
    security: Security = Field(default_factory=Security)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    agents: dict[str, ImportStringWithParams[Agent]] = Field(
        default_factory=lambda: {  # type: ignore[arg-type]
            "default": ImportStringWithParams(cls_or_fn=DefaultAgent),
        }
    )


class Config(BaseConfig):
    """ravnar configuration"""

    model_config = SettingsConfigDict(
        env_prefix="RAVNAR_",
        env_nested_delimiter="__",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_files = [
            (p / "config").with_suffix(s)
            for p in [Path("/etc/ravnar"), Path.home() / ".config" / "ravnar", Path.cwd()]
            for s in [".yml", ".yaml"]
        ]
        if (yaml_file := os.environ.get("RAVNAR_CONFIG")) is not None:
            yaml_files.append(Path(yaml_file).expanduser().resolve())
        return init_settings, env_settings, YamlConfigSettingsSource(settings_cls, yaml_files, deep_merge=True)

    @model_validator(mode="before")
    @classmethod
    def _maybe_set_import_path(self, data: Any) -> Any:
        if (ravnar_path := os.environ.get("RAVNARPATH")) is not None:
            sys.path[:0] = [str(Path(p).expanduser().resolve()) for p in ravnar_path.split(os.pathsep) if p]
        return data

    @classmethod
    def parse(cls, obj: dict[str, Any] | None = None) -> Self:
        if obj is None:
            obj = {}
        return cls.model_validate(obj)
