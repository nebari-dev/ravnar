from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Self, TypeVar

import l2sl
from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource
from upath import UPath

from _ravnar.utils import ImportStringWithParams, render_template

from .agents import Agent, DefaultAgent
from .authenticators import Authenticator

T = TypeVar("T")


def interactive_session() -> bool:
    return sys.stdout.isatty()


class RenderableMixin:
    @field_validator("*", mode="before")
    @classmethod
    def _render_templates(cls, data: Any) -> Any:
        return render_template(data, context=dict(os.environ))


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


class SecurityConfig(BaseModel, RenderableMixin):
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
    enabled: bool = True
    database_dsn: str = Field(default_factory=lambda: f"sqlite:///{_local_storage() / 'state.db'}")
    file_storage_path: UPath = Field(default_factory=lambda: UPath(_local_storage() / "files"))


class DynamicAgentConfig(BaseModel, RenderableMixin):
    enabled: bool = False
    allowed_env_vars: list[str] = Field(default_factory=list)


class AgentConfig(BaseModel, RenderableMixin):
    static: dict[str, ImportStringWithParams[Agent]] = Field(
        default_factory=lambda: {  # type: ignore[arg-type]
            "default": ImportStringWithParams(cls_or_fn=DefaultAgent),
        }
    )
    dynamic: DynamicAgentConfig = Field(default_factory=DynamicAgentConfig)

    @model_validator(mode="after")
    def _ensure_not_agentless(self) -> Self:
        if not self.static and not self.dynamic.enabled:
            raise ValueError("At least one static agent must be configured, or dynamic agents must be enabled.")
        return self


class BaseConfig(BaseSettings, RenderableMixin):
    server: ServerConfig = Field(default_factory=ServerConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    agents: AgentConfig = Field(default_factory=AgentConfig)


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
