from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Self, TypeVar

import l2sl
import opentelemetry.sdk.trace
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource
from upath import UPath

from _ravnar.agents import Agent
from _ravnar.authenticators import Authenticator
from _ravnar.utils import ImportStringWithParams, normalize_hostname, render_template

T = TypeVar("T")


def interactive_session() -> bool:
    return sys.stdout.isatty()


def _validate_allowlist_wildcard(allowlist: list[str]) -> list[str]:
    if "*" in allowlist and len(allowlist) > 1:
        raise ValueError('Wildcard "*" must be the sole allowlist entry. It cannot be combined with other entries.')
    return allowlist


Allowlist = Annotated[list[str], AfterValidator(_validate_allowlist_wildcard)]


class RenderableConfigMixin:
    @field_validator("*", mode="before")
    @classmethod
    def _render_templates(cls, data: Any) -> Any:
        return render_template(data, context=dict(os.environ))


class ServerConfig(BaseModel, RenderableConfigMixin):
    hostname: str = "127.0.0.1"
    port: int = 8000
    proxy_headers: bool = False
    forwarded_allow_ips: list[str] = Field(default_factory=lambda: ["*"])
    root_path: str = ""


class LoggingConfig(BaseModel, RenderableConfigMixin):
    level: l2sl.LogLevel = l2sl.LogLevel("info")
    as_json: bool = Field(default_factory=lambda: not interactive_session())


def default_tracing_span_processors() -> list[ImportStringWithParams[opentelemetry.sdk.trace.SpanProcessor]]:
    if not interactive_session():
        return []

    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from ravnar.observability import StructlogSpanExporter

    return [
        ImportStringWithParams(
            cls_or_fn=SimpleSpanProcessor,
            params={"span_exporter": ImportStringWithParams(cls_or_fn=StructlogSpanExporter)},
        )
    ]


class TracingConfig(BaseModel, RenderableConfigMixin):
    span_processors: list[ImportStringWithParams[opentelemetry.sdk.trace.SpanProcessor]] = Field(
        default_factory=default_tracing_span_processors
    )


class ObservabilityConfig(BaseModel, RenderableConfigMixin):
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    tracing: TracingConfig = Field(default_factory=TracingConfig)


class CORSConfig(BaseModel, RenderableConfigMixin):
    allowed_origins: Allowlist = Field(default_factory=lambda: ["*"])
    allowed_headers: Allowlist = Field(default_factory=list)


class SecurityConfig(BaseModel, RenderableConfigMixin):
    authenticator: ImportStringWithParams[Authenticator] | None = None
    cors: CORSConfig = Field(default_factory=CORSConfig)


def _local_storage() -> Path:
    if (ls := os.environ.get("RAVNAR_LOCAL_STORAGE")) is None:
        p = Path.cwd() / ".ravnar_local"
    else:
        p = Path(ls).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


class DatabaseConfig(BaseModel, RenderableConfigMixin):
    dsn: str = Field(default_factory=lambda: f"sqlite:///{_local_storage() / 'state.db'}")


class URLDataSourceConfig(BaseModel, RenderableConfigMixin):
    enabled: bool = False
    allowed_hostnames: Allowlist = Field(default_factory=list)
    timeout: timedelta = timedelta(seconds=30)

    @field_validator("allowed_hostnames", mode="after")
    @classmethod
    def _normalize_hostnames(cls, allowlist: list[str]) -> list[str]:
        if "*" in allowlist:
            return allowlist

        return [normalize_hostname(hostname) for hostname in allowlist]


class FileStorageConfig(BaseModel, RenderableConfigMixin):
    path: UPath = Field(default_factory=lambda: UPath(_local_storage() / "files"))
    url_data_source: URLDataSourceConfig = Field(default_factory=URLDataSourceConfig)


class StorageConfig(BaseModel, RenderableConfigMixin):
    enabled: bool = True
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    files: FileStorageConfig = Field(default_factory=FileStorageConfig)


class DynamicAgentConfig(BaseModel, RenderableConfigMixin):
    enabled: bool = False
    allowed_env_vars: Allowlist = Field(default_factory=list)


def default_static_agents() -> dict[str, ImportStringWithParams[Agent]]:
    from ravnar.agents import DefaultAgent

    return {"default": ImportStringWithParams(cls_or_fn=DefaultAgent)}


class AgentConfig(BaseModel, RenderableConfigMixin):
    static: dict[str, ImportStringWithParams[Agent]] = Field(default_factory=default_static_agents)
    dynamic: DynamicAgentConfig = Field(default_factory=DynamicAgentConfig)

    @model_validator(mode="after")
    def _ensure_not_agentless(self) -> Self:
        if not self.static and not self.dynamic.enabled:
            raise ValueError("At least one static agent must be configured, or dynamic agents must be enabled.")
        return self


class BaseConfig(BaseSettings, RenderableConfigMixin):
    server: ServerConfig = Field(default_factory=ServerConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
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
