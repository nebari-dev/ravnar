from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any, Generic, Literal, TypeVar, get_args

import ag_ui.core
from sqlalchemy import ForeignKey, inspect, types
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedAsDataclass, mapped_column, relationship


class Json(types.TypeDecorator):
    """Universal JSON type which stores values as text.

    This is needed because sqlalchemy.types.JSON only works for a limited subset of
    databases.
    """

    impl = types.Text

    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return value

        return json.dumps(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> Any:
        if value is None:
            return value

        return json.loads(value)


class UtcAwareDateTime(types.TypeDecorator):
    """UTC timezone aware datetime type.

    This is needed because sqlalchemy.types.DateTime(timezone=True) does not
    consistently store the timezone.
    """

    impl = types.DateTime

    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None:
            assert value.tzinfo == UTC

        return value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None

        return value.replace(tzinfo=UTC)


class Base(MappedAsDataclass, DeclarativeBase):
    def __repr__(self) -> str:
        unloaded_fields = inspect(self).unloaded
        field_values = [
            f"{field.name}={'<unloaded>' if field.name in unloaded_fields else repr(getattr(self, field.name))}"
            for field in dataclasses.fields(self)
        ]
        return f"{type(self).__name__}({','.join(field_values)})"


TOrm = TypeVar("TOrm", bound=Base)


@dataclasses.dataclass(kw_only=True)
class Page(Generic[TOrm]):
    page_size: int
    page_number: int
    total_count: int
    page_count: int
    items: list[TOrm]


State = Any


class Thread(Base, kw_only=True, repr=False):
    __tablename__ = "threads"

    id: Mapped[str] = mapped_column(primary_key=True)

    user_id: Mapped[str]
    agent_id: Mapped[str]
    name: Mapped[str | None]

    created_at: Mapped[datetime] = mapped_column(UtcAwareDateTime)
    updated_at: Mapped[datetime] = mapped_column(UtcAwareDateTime)

    state: Mapped[State] = mapped_column(Json, nullable=True)
    messages: Mapped[list[Message]] = relationship(
        "Message",
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="[Message.created_at.asc(), Message.id]",
    )


class Message(Base, kw_only=True, repr=False):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(primary_key=True)

    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id"), index=True)
    thread: Mapped[Thread] = relationship(init=False)

    created_at: Mapped[datetime] = mapped_column(UtcAwareDateTime)
    updated_at: Mapped[datetime | None] = mapped_column(UtcAwareDateTime, default=None)

    role: Mapped[ag_ui.core.Role] = mapped_column(
        types.Enum(*get_args(ag_ui.core.Role), name="message_role", native_enum=False)
    )

    content: Mapped[Any] = mapped_column(Json, nullable=True, default=None)
    name: Mapped[str | None] = mapped_column(default=None)
    encrypted_value: Mapped[str | None] = mapped_column(default=None)

    __mapper_args__ = {"polymorphic_on": "role", "with_polymorphic": "*"}


class DeveloperMessage(Message, kw_only=True, repr=False):
    __mapper_args__ = {"polymorphic_identity": "developer"}

    role: Mapped[Literal["developer"]] = mapped_column(types.String, default="developer", use_existing_column=True)
    content: Mapped[str] = mapped_column(use_existing_column=True, nullable=True)


class SystemMessage(Message, kw_only=True, repr=False):
    __mapper_args__ = {"polymorphic_identity": "system"}

    role: Mapped[Literal["system"]] = mapped_column(types.String, default="system", use_existing_column=True)
    content: Mapped[str] = mapped_column(use_existing_column=True, nullable=True)


class AssistantMessage(Message, kw_only=True, repr=False):
    __mapper_args__ = {"polymorphic_identity": "assistant"}

    role: Mapped[Literal["assistant"]] = mapped_column(types.String, default="assistant", use_existing_column=True)
    content: Mapped[str | None] = mapped_column(use_existing_column=True)
    tool_calls: Mapped[list[ToolCall]] = relationship(
        "ToolCall",
        back_populates="assistant_message",
        cascade="all, delete-orphan",
        foreign_keys="[ToolCall.assistant_message_id]",
        lazy="selectin",
    )


class UserMessage(Message, kw_only=True, repr=False):
    __mapper_args__ = {"polymorphic_identity": "user"}

    role: Mapped[Literal["user"]] = mapped_column(types.String, default="user", use_existing_column=True)
    content: Mapped[str] = mapped_column(use_existing_column=True, nullable=True)


class ToolMessage(Message, kw_only=True, repr=False):
    __mapper_args__ = {"polymorphic_identity": "tool"}

    role: Mapped[Literal["tool"]] = mapped_column(types.String, default="tool", use_existing_column=True)
    content: Mapped[str] = mapped_column(use_existing_column=True, nullable=True)
    tool_call: Mapped[ToolCall] = relationship(
        "ToolCall",
        back_populates="tool_message",
        uselist=False,
        foreign_keys="[ToolCall.tool_message_id]",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    error: Mapped[str | None]
    encrypted_value: Mapped[str | None] = mapped_column(use_existing_column=True)


class ActivityMessage(Message, kw_only=True, repr=False):
    __mapper_args__ = {"polymorphic_identity": "activity"}

    role: Mapped[Literal["activity"]] = mapped_column(types.String, default="activity", use_existing_column=True)
    content: Mapped[dict[str, Any]] = mapped_column(use_existing_column=True, nullable=True)
    activity_type: Mapped[str | None]


class ReasoningMessage(Message, kw_only=True, repr=False):
    __mapper_args__ = {"polymorphic_identity": "reasoning"}

    role: Mapped[Literal["reasoning"]] = mapped_column(types.String, default="reasoning", use_existing_column=True)
    content: Mapped[str] = mapped_column(use_existing_column=True, nullable=True)


class ToolCall(Base, kw_only=True, repr=False):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(primary_key=True)

    assistant_message_id: Mapped[str] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    assistant_message: Mapped[AssistantMessage] = relationship(
        init=False, back_populates="tool_calls", foreign_keys=[assistant_message_id]
    )

    tool_message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    tool_message: Mapped[ToolMessage | None] = relationship(
        init=False, back_populates="tool_call", foreign_keys=[tool_message_id]
    )

    name: Mapped[str]
    arguments: Mapped[str]
    encrypted_value: Mapped[str | None]
