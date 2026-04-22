from __future__ import annotations

__all__ = [
    "APIConfig",
    "AgentConfig",
    "AugmentedActivityMessage",
    "AugmentedAssistantMessage",
    "AugmentedDeveloperMessage",
    "AugmentedMessage",
    "AugmentedReasoningMessage",
    "AugmentedSystemMessage",
    "AugmentedToolMessage",
    "AugmentedUserMessage",
    "CreateRunData",
    "CreateThreadData",
    "DeleteThreadsData",
    "Event",
    "QuickPrompt",
    "RenameThreadData",
    "Thread",
    "FileParameters",
    "File"
]

import uuid
from datetime import datetime
from typing import Annotated, Any

import ag_ui.core
from pydantic import Field, model_validator

from _ravnar import orm
from _ravnar.utils import now
from typing import Self
from upath import UPath

from .misc import BaseModel


class QuickPrompt(BaseModel):
    title: str
    description: str | None = None
    prompt: str


class AgentConfig(BaseModel):
    id: str
    capabilities: ag_ui.core.AgentCapabilities
    quick_prompts: list[QuickPrompt]


class APIConfig(BaseModel):
    agents: list[AgentConfig]


class Thread(BaseModel):
    id: str
    name: str | None = None
    agent_id: str
    created_at: datetime
    updated_at: datetime


class AugmentedMessageMixin(ag_ui.core.BaseMessage):
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime | None = None

    @classmethod
    def _convert_orm_tool_call(cls, tool_call: orm.ToolCall) -> ag_ui.core.ToolCall:
        return ag_ui.core.ToolCall(
            id=tool_call.id,
            function=ag_ui.core.FunctionCall(name=tool_call.name, arguments=tool_call.arguments),
            encrypted_value=tool_call.encrypted_value,
        )


class AugmentedDeveloperMessage(ag_ui.core.DeveloperMessage, AugmentedMessageMixin):
    pass


class AugmentedSystemMessage(ag_ui.core.SystemMessage, AugmentedMessageMixin):
    pass


class AugmentedAssistantMessage(ag_ui.core.AssistantMessage, AugmentedMessageMixin):
    @model_validator(mode="before")
    @classmethod
    def _convert_orm(cls, obj: Any) -> Any:
        if isinstance(obj, orm.AssistantMessage) and obj.tool_calls:
            tool_calls = [cls._convert_orm_tool_call(tc) for tc in obj.tool_calls]
            obj = {field: getattr(obj, field) for field in cls.model_fields if field not in {"tool_calls"}}
            obj["tool_calls"] = tool_calls
        return obj


class AugmentedUserMessage(ag_ui.core.UserMessage, AugmentedMessageMixin):
    pass


class AugmentedToolMessage(ag_ui.core.ToolMessage, AugmentedMessageMixin):  # type: ignore[misc]
    @model_validator(mode="before")
    @classmethod
    def _convert_orm(cls, obj: Any) -> Any:
        if isinstance(obj, orm.ToolMessage) and obj.tool_call is not None:
            tool_call = cls._convert_orm_tool_call(obj.tool_call)
            obj = {field: getattr(obj, field) for field in cls.model_fields if field not in {"tool_call_id"}}
            obj["tool_call_id"] = tool_call.id
        return obj


class AugmentedActivityMessage(ag_ui.core.ActivityMessage, AugmentedMessageMixin):  # type: ignore[misc]
    pass


class AugmentedReasoningMessage(ag_ui.core.ReasoningMessage, AugmentedMessageMixin):  # type: ignore[misc]
    pass


AugmentedMessage = Annotated[
    AugmentedDeveloperMessage
    | AugmentedSystemMessage
    | AugmentedAssistantMessage
    | AugmentedUserMessage
    | AugmentedToolMessage
    | AugmentedActivityMessage
    | AugmentedReasoningMessage,
    Field(discriminator="role"),
]

Event = Annotated[ag_ui.core.Event, Field(title="Event")]


class CreateThreadData(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str | None = None
    agent_id: str


class CreateRunData(BaseModel):
    messages: list[AugmentedUserMessage | AugmentedToolMessage]
    tools: list[ag_ui.core.Tool] = Field(default_factory=list)
    context: list[ag_ui.core.Context] = Field(default_factory=list)
    forwarded_props: Any = None


class RenameThreadData(BaseModel):
    name: str


class DeleteThreadsData(BaseModel):
    ids: list[str] | None = None

class FileParameters(BaseModel):
    content_type: str
    size: int | None = None
    name: str | None = None
    external_url: UPath | None = None


class File(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    # mime_type, optional??? Can we get the mime type from the URL?
    content_type: str
    metadata: dict[str, Any] | None
    # maybe source with str | None for URLS?

    # size: int | None
    # name: str | None
    # external_url: UPath | None = Field(exclude=True)

    @classmethod
    def from_params(cls, params: FileParameters) -> Self:
        return cls(
            content_type=params.content_type,
            size=params.size,
            name=params.name,
            external_url=params.external_url,
        )