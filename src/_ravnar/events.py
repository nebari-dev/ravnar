from __future__ import annotations

import dataclasses
import enum
import time
import uuid
from collections.abc import AsyncIterable, AsyncIterator
from datetime import UTC, datetime
from typing import Any, TypeVar, cast

import ag_ui.core
import ag_ui.encoder
import jsonpatch
import pydantic
import structlog
from opentelemetry import trace

from _ravnar.file_storage import WrappedMetadata
from _ravnar.observability import LazyValue

from . import orm
from .utils import now

tracer = trace.get_tracer(__name__)

TEvent = TypeVar("TEvent", bound=ag_ui.core.Event)


class RunProgress(enum.Enum):
    NOT_STARTED = enum.auto()
    STARTED = enum.auto()
    FINISHED = enum.auto()


def parse_timestamp(timestamp: int | None) -> datetime:
    if timestamp is None:
        return now()

    return datetime.fromtimestamp(timestamp / 1_000, tz=UTC)


@dataclasses.dataclass
class TextMessageData:
    created_at: datetime
    message_id: str
    content_deltas: list[str] = dataclasses.field(default_factory=list)
    finished: bool = False


@dataclasses.dataclass
class ToolCallData:
    created_at: datetime
    tool_call_id: str
    tool_call_name: str
    parent_message_id: str
    arguments_delta: list[str] = dataclasses.field(default_factory=list)
    finished: bool = False


@dataclasses.dataclass
class ToolResultData:
    created_at: datetime
    message_id: str
    tool_call_id: str
    content: str


@dataclasses.dataclass
class ReasoningData:
    created_at: datetime
    message_id: str
    content_deltas: list[str] = dataclasses.field(default_factory=list)
    finished: bool = False


class EventProcessor:
    def __init__(
        self,
        *,
        thread_id: str,
        run_id: str,
        parent_run_id: str | None,
        state: ag_ui.core.State,
        messages: list[ag_ui.core.Message],
    ):
        self._thread_id = thread_id
        self._run_id = run_id
        self._parent_run_id = parent_run_id

        self._state = state
        self._messages = self._convert_messages(messages)

        self._progress = RunProgress.NOT_STARTED
        self._text_message_data: dict[str, TextMessageData] = {}
        self._tool_call_data: dict[str, ToolCallData] = {}
        self._tool_result_data: dict[str, ToolResultData] = {}
        self._reasoning_data: dict[str, ReasoningData] = {}
        self._thinking_message_id: str | None = None

        self._logger = structlog.get_logger(thread_id=thread_id, run_id=run_id, parent_run_id=parent_run_id)

    def _convert_messages(
        self, messages: list[ag_ui.core.Message], *, updated_at: datetime | None = None
    ) -> dict[str, orm.Message]:
        tool_calls = {
            tc.id: orm.ToolCall(
                id=tc.id,
                assistant_message_id=m.id,
                tool_message_id=None,
                name=tc.function.name,
                arguments=tc.function.arguments,
                encrypted_value=tc.encrypted_value,
            )
            for m in messages
            if isinstance(m, ag_ui.core.AssistantMessage)
            for tc in m.tool_calls or []
        }

        converted_messages: dict[str, orm.Message] = {}
        for m in messages:
            cls = orm.Message.__mapper__.polymorphic_map[m.role].class_

            data: dict[str, Any]
            match m:
                case ag_ui.core.UserMessage():
                    assert not isinstance(m.content, str)
                    input_contents: list[orm.InputContent] = []
                    for i, c in enumerate(m.content):
                        assert not isinstance(c, ag_ui.core.BinaryInputContent)
                        text: str | None
                        file_id: uuid.UUID | None
                        if isinstance(c, ag_ui.core.TextInputContent):
                            text = c.text
                            file_id = None
                        else:
                            assert isinstance(c.source, ag_ui.core.InputContentDataSource)
                            metadata = WrappedMetadata.model_validate(c.metadata)
                            text = None
                            file_id = metadata.file_id
                        input_contents.append(
                            orm.InputContent(user_message_id=m.id, index=i, text=text, file_id=file_id)
                        )
                    data = {**m.model_dump(exclude={"content"}), "input_contents": input_contents}
                    print()
                case ag_ui.core.AssistantMessage():
                    data = {
                        **m.model_dump(exclude={"tool_calls"}),
                        "tool_calls": [tool_calls[tc.id] for tc in m.tool_calls or []],
                    }
                case ag_ui.core.ToolMessage():
                    tool_call = tool_calls[m.tool_call_id]
                    tool_call.tool_message_id = m.id
                    data = {**m.model_dump(exclude={"tool_call_id"}), "tool_call": tool_call}
                case _:
                    data = m.model_dump()

            if updated_at is not None:
                data["updated_at"] = updated_at
            data["thread_id"] = self._thread_id

            converted_messages[m.id] = cls(**data)

        return converted_messages

    async def process_event_stream(
        self, event_stream: AsyncIterable[ag_ui.core.Event]
    ) -> AsyncIterator[ag_ui.core.Event]:
        span = tracer.start_span("EventProcessor.run")
        span.set_attribute("thread_id", self._thread_id)
        span.set_attribute("run_id", self._run_id)
        span.set_attribute("parent_run_id", self._parent_run_id)

        events = aiter(event_stream)
        try:
            while True:
                try:
                    event = await anext(events)
                except StopAsyncIteration:
                    return
                except Exception as exc:
                    message = "unhandled exception in agent"
                    self._logger.exception(message)
                    span.record_exception(exc)
                    span.set_status(trace.StatusCode.ERROR, description=message)

                    if self._progress == RunProgress.STARTED:
                        yield ag_ui.core.RunErrorEvent(
                            timestamp=int(time.time()), message=message, code="ravnar:unhandled-exception"
                        )

                    return

                processed_event = self._process_event(event)
                if processed_event is not None:
                    yield processed_event
        finally:
            span.end()

    def _trace_event(self, event: ag_ui.core.Event, **attributes: Any) -> ag_ui.core.Event:
        span = trace.get_current_span()
        span.add_event(event.type, attributes={"type": event.type, **attributes})
        return event

    def _process_event(self, event: ag_ui.core.Event) -> ag_ui.core.Event | None:
        logger = self._logger.bind(event_type=event.type)

        logger.debug("event", agent_event=LazyValue(event.model_dump))

        if self._progress == RunProgress.NOT_STARTED and not isinstance(event, ag_ui.core.RunStartedEvent):
            self._trace_event(event, state="dropped", reason="not started")
            return None

        if self._progress == RunProgress.FINISHED:
            self._trace_event(event, state="dropped", reason="finished")
            return None

        match event:
            # lifecycle events
            case ag_ui.core.RunStartedEvent():
                if self._progress != RunProgress.NOT_STARTED:
                    self._trace_event(event, state="dropped", reason="already started")
                    return None

                if (
                    event.thread_id != self._thread_id
                    or event.run_id != self._run_id
                    or event.parent_run_id != self._parent_run_id
                ):
                    lifecycle_data = event.model_dump(include={"thread_id", "run_id", "parent_run_id"}, mode="json")
                    event = self._override_event(
                        event, thread_id=self._thread_id, run_id=self._run_id, parent_run_id=self._parent_run_id
                    )
                    attributes = {
                        "state": "overridden",
                        "reason": "mismatching lifecycle data",
                        "lifecycle_data": lifecycle_data,
                    }

                else:
                    attributes = {}

                self._progress = RunProgress.STARTED
                return self._trace_event(event, **attributes)

            case ag_ui.core.RunFinishedEvent():
                if event.thread_id != self._thread_id or event.run_id != self._run_id:
                    lifecycle_data = event.model_dump(include={"thread_id", "run_id"}, mode="json")
                    event = self._override_event(event, thread_id=self._thread_id, run_id=self._run_id)
                    self._progress = RunProgress.FINISHED
                    return self._trace_event(
                        event,
                        state="overridden",
                        reason="mismatching lifecycle data",
                        lifecycle_data=lifecycle_data,
                    )
                self._progress = RunProgress.FINISHED
                return self._trace_event(event)
            case ag_ui.core.RunErrorEvent():
                span = trace.get_current_span()
                span.set_status(
                    trace.StatusCode.ERROR,
                    description=f"{event.message} ({event.code})",
                )
                self._progress = RunProgress.FINISHED
                return self._trace_event(event)
            # text message events
            case ag_ui.core.TextMessageStartEvent():
                overridden = event.message_id in self._text_message_data
                self._text_message_data[event.message_id] = TextMessageData(
                    created_at=parse_timestamp(event.timestamp),
                    message_id=event.message_id,
                )
                return self._trace_event(
                    event,
                    **{
                        "state": "overridden",
                        "reason": "already started",
                        "message_id": event.message_id,
                    }
                    if overridden
                    else {},
                )
                return self._trace_event(event)
            case ag_ui.core.TextMessageContentEvent():
                tmd = self._text_message_data.get(event.message_id)
                if tmd is None:
                    self._trace_event(event, state="dropped", reason="not started", message_id=event.message_id)
                    return None
                if tmd.finished:
                    self._trace_event(event, state="dropped", reason="finished", message_id=event.message_id)
                    return None
                tmd.content_deltas.append(event.delta)
                return self._trace_event(event)
            case ag_ui.core.TextMessageEndEvent():
                tmd = self._text_message_data.get(event.message_id)
                if tmd is None:
                    self._trace_event(event, state="dropped", reason="not started", message_id=event.message_id)
                    return None
                if tmd.finished:
                    self._trace_event(event, state="dropped", reason="already finished", message_id=event.message_id)
                    return None
                tmd.finished = True
                return self._trace_event(event)
            # tool call events
            case ag_ui.core.ToolCallStartEvent():
                overridden = event.tool_call_id in self._tool_call_data
                if event.parent_message_id is not None:
                    parent_message_id = event.parent_message_id
                elif self._text_message_data:
                    parent_message_id = next(reversed(self._text_message_data.values())).message_id
                else:
                    parent_message_id = self._new_id()
                    self._text_message_data[parent_message_id] = TextMessageData(
                        created_at=parse_timestamp(event.timestamp),
                        message_id=parent_message_id,
                        finished=True,
                    )
                self._tool_call_data[event.tool_call_id] = ToolCallData(
                    created_at=parse_timestamp(event.timestamp),
                    tool_call_id=event.tool_call_id,
                    tool_call_name=event.tool_call_name,
                    parent_message_id=parent_message_id,
                )
                return self._trace_event(
                    event,
                    **{
                        "state": "overridden",
                        "reason": "already started",
                        "tool_call_id": event.tool_call_id,
                        "tool_call_name": event.tool_call_name,
                        "parent_message_id": event.parent_message_id,
                    }
                    if overridden
                    else {},
                )
            case ag_ui.core.ToolCallArgsEvent():
                tcd = self._tool_call_data.get(event.tool_call_id)
                if tcd is None:
                    self._trace_event(event, state="dropped", reason="not started", tool_call_id=event.tool_call_id)
                    return None
                if tcd.finished:
                    self._trace_event(event, state="dropped", reason="finished", tool_call_id=event.tool_call_id)
                    return None
                tcd.arguments_delta.append(event.delta)
                return self._trace_event(event)
            case ag_ui.core.ToolCallEndEvent():
                tcd = self._tool_call_data.get(event.tool_call_id)
                if tcd is None:
                    self._trace_event(event, state="dropped", reason="not started", tool_call_id=event.tool_call_id)
                    return None
                if tcd.finished:
                    self._trace_event(
                        event, state="dropped", reason="already finished", tool_call_id=event.tool_call_id
                    )
                    return None
                tcd.finished = True
                return self._trace_event(event)
            case ag_ui.core.ToolCallResultEvent():
                overridden = event.message_id in self._tool_result_data
                self._tool_result_data[event.message_id] = ToolResultData(
                    created_at=parse_timestamp(event.timestamp),
                    message_id=event.message_id,
                    tool_call_id=event.tool_call_id,
                    content=event.content,
                )
                return self._trace_event(
                    event,
                    **{
                        "state": "overridden",
                        "reason": "already received",
                        "message_id": event.message_id,
                        "tool_call_id": event.tool_call_id,
                    }
                    if overridden
                    else {},
                )
            # state management events
            case ag_ui.core.StateSnapshotEvent():
                self._state = event.snapshot
                return self._trace_event(event)
            case ag_ui.core.StateDeltaEvent():
                new_state = self._apply_jsonpatch(self._state, event.delta)
                if new_state is None:
                    self._trace_event(event, state="dropped", reason="invalid JSON patches")
                    return None
                self._state = new_state
                return self._trace_event(event)
            # case ag_ui.core.MessagesSnapshotEvent():
            #     self._messages = self._convert_messages(event.messages, updated_at=parse_timestamp(event.timestamp))
            #     return ag_ui.core.MessagesSnapshotEvent(messages=messages, timestamp=event.timestamp, raw_event=event)
            # activity events
            case ag_ui.core.ActivitySnapshotEvent():
                if event.message_id in self._messages and not event.replace:
                    self._trace_event(
                        event,
                        state="dropped",
                        reason="message already exist",
                        message_id=event.message_id,
                        replace=event.replace,
                    )
                    return None
                self._messages[event.message_id] = orm.ActivityMessage(
                    id=event.message_id,
                    thread_id=self._thread_id,
                    created_at=parse_timestamp(event.timestamp),
                    content=event.content,
                    activity_type=event.activity_type,
                )
                return self._trace_event(event)
            case ag_ui.core.ActivityDeltaEvent():
                message = self._messages.get(event.message_id)
                if message is None:
                    self._trace_event(
                        event,
                        state="dropped",
                        reason="message does not exist",
                        message_id=event.message_id,
                    )
                    return None
                if not isinstance(message, orm.ActivityMessage):
                    self._trace_event(
                        event,
                        state="dropped",
                        reason="mismatching message role",
                        message_role=message.role,
                    )
                    return None

                overridden = False
                if event.activity_type != message.activity_type:
                    overridden = True
                    original_activity_type = event.activity_type
                    event = self._override_event(event, activity_type=message.activity_type)

                content = self._apply_jsonpatch(message.content, event.patch)
                if content is None:
                    self._trace_event(event, state="dropped", reason="invalid JSON patches")
                    return None

                message.updated_at = parse_timestamp(event.timestamp)
                message.content = content
                if overridden:
                    return self._trace_event(
                        event,
                    )
                return self._trace_event(
                    event,
                    **{
                        "state": "overridden",
                        "reason": "mismatching activity type",
                        "message_activity_type": message.activity_type,
                        "activity_type": original_activity_type,
                    }
                    if overridden
                    else {},
                )
            # special events
            # reasoning events
            case ag_ui.core.ReasoningStartEvent() | ag_ui.core.ReasoningEndEvent():
                # passthrough events for subscribers that do not create messages
                return self._trace_event(event)
            case ag_ui.core.ReasoningMessageStartEvent():
                overridden = event.message_id in self._reasoning_data
                self._reasoning_data[event.message_id] = ReasoningData(
                    created_at=parse_timestamp(event.timestamp),
                    message_id=event.message_id,
                )
                if overridden:
                    return self._trace_event(
                        event,
                        state="overridden",
                        reason="already started",
                        message_id=event.message_id,
                    )
                return self._trace_event(
                    event,
                    **{
                        "state": "overridden",
                        "reason": "already started",
                        "message_id": event.message_id,
                    }
                    if overridden
                    else {},
                )
            case ag_ui.core.ReasoningMessageContentEvent():
                rd = self._reasoning_data.get(event.message_id)
                if rd is None:
                    self._trace_event(event, state="dropped", reason="not started", message_id=event.message_id)
                    return None
                if rd.finished:
                    self._trace_event(event, state="dropped", reason="finished", message_id=event.message_id)
                    return None
                rd.content_deltas.append(event.delta)
                return self._trace_event(event)
            case ag_ui.core.ReasoningMessageEndEvent():
                rd = self._reasoning_data.get(event.message_id)
                if rd is None:
                    self._trace_event(event, state="dropped", reason="not started", message_id=event.message_id)
                    return None
                if rd.finished:
                    self._trace_event(event, state="dropped", reason="already finished", message_id=event.message_id)
                    return None
                rd.finished = True
                return self._trace_event(event)
            case (
                ag_ui.core.ThinkingStartEvent()
                | ag_ui.core.ThinkingEndEvent()
                | ag_ui.core.ThinkingTextMessageStartEvent()
                | ag_ui.core.ThinkingTextMessageContentEvent()
                | ag_ui.core.ThinkingTextMessageEndEvent()
            ):
                if isinstance(event, ag_ui.core.ThinkingStartEvent):
                    # FIXME: check if not-None
                    self._thinking_message_id = self._new_id()
                else:
                    # FIXME: check if None
                    pass
                message_id = self._thinking_message_id
                if isinstance(event, ag_ui.core.ThinkingEndEvent):
                    self._thinking_message_id = None

                event_data = {
                    "type": {
                        ag_ui.core.EventType.THINKING_START: ag_ui.core.EventType.REASONING_START,
                        ag_ui.core.EventType.THINKING_END: ag_ui.core.EventType.REASONING_END,
                        ag_ui.core.EventType.THINKING_TEXT_MESSAGE_START: ag_ui.core.EventType.REASONING_MESSAGE_START,
                        ag_ui.core.EventType.THINKING_TEXT_MESSAGE_CONTENT: ag_ui.core.EventType.REASONING_MESSAGE_CONTENT,
                        ag_ui.core.EventType.THINKING_TEXT_MESSAGE_END: ag_ui.core.EventType.REASONING_MESSAGE_END,
                    }[event.type],
                    "message_id": message_id,
                    **event.model_dump(exclude={"type"}),
                }
                if isinstance(event, ag_ui.core.ThinkingTextMessageStartEvent):
                    event_data["role"] = "reasoning"
                self._trace_event(event, state="replaced", reason="deprecated")
                event = pydantic.TypeAdapter(ag_ui.core.Event).validate_python(event_data)
                return self._process_event(event)
            case _:
                return self._trace_event(
                    event,
                    state="passed through",
                    reason="unknown event type",
                    agent_event=event.model_dump(),
                )

    @staticmethod
    def _new_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def _override_event(event: TEvent, **replace: Any) -> TEvent:
        return cast(TEvent, event.model_copy(update={"raw_event": event, **replace}))

    def _apply_jsonpatch(self, document: dict[str, Any], patches: list[Any]) -> Any:
        try:
            # this cannot be in-place as it will not roll back in case of an exception
            return jsonpatch.JsonPatch(patches).apply(document, in_place=False)
        except jsonpatch.JsonPatchException:
            return None

    def extract(self) -> tuple[orm.State, list[orm.Message]]:
        return self._state, self._extract_messages()

    def _extract_messages(self) -> list[orm.Message]:
        tool_calls: dict[str, orm.ToolCall] = {}
        tool_calls_created_at: dict[str, datetime] = {}
        grouped_tool_calls: dict[str, list[orm.ToolCall]] = {}
        for tcd in self._tool_call_data.values():
            if not tcd.finished:
                span = trace.get_current_span()
                span.add_event(
                    "unfinished_tool_call",
                    attributes={
                        "tool_call_id": tcd.tool_call_id,
                        "tool_call_name": tcd.tool_call_name,
                        "parent_message_id": tcd.parent_message_id,
                    },
                )
                self._logger.warn(
                    "tool call",
                    state="dropped",
                    reason="unfinished",
                    tool_call_id=tcd.tool_call_id,
                    tool_call_name=tcd.tool_call_name,
                    parent_message_id=tcd.parent_message_id,
                )
                continue

            tool_call = orm.ToolCall(
                id=tcd.tool_call_id,
                assistant_message_id=tcd.parent_message_id,
                tool_message_id=None,
                name=tcd.tool_call_name,
                arguments="".join(tcd.arguments_delta),
                # FIXME
                encrypted_value=None,
            )
            tool_calls[tool_call.id] = tool_call
            tool_calls_created_at[tool_call.id] = tcd.created_at
            grouped_tool_calls.setdefault(tool_call.assistant_message_id, []).append(tool_call)

        messages: list[orm.Message] = list(self._messages.values())

        for tmd in self._text_message_data.values():
            if not tmd.finished:
                span = trace.get_current_span()
                span.add_event(
                    "unfinished_text_message",
                    attributes={"message_id": tmd.message_id},
                )
                self._logger.warn("text message", state="dropped", reason="unfinished", message_id=tmd.message_id)
                continue

            messages.append(
                orm.AssistantMessage(
                    id=tmd.message_id,
                    thread_id=self._thread_id,
                    created_at=tmd.created_at,
                    content="".join(tmd.content_deltas) or None,
                    tool_calls=grouped_tool_calls.pop(tmd.message_id, []),
                )
            )

        for mid, tcs in grouped_tool_calls.items():
            messages.append(
                orm.AssistantMessage(
                    id=mid,
                    thread_id=self._thread_id,
                    created_at=min(tool_calls_created_at[tc.id] for tc in tcs),
                    content=None,
                    tool_calls=tcs,
                )
            )

        for rd in self._reasoning_data.values():
            if not rd.finished:
                span = trace.get_current_span()
                span.add_event(
                    "unfinished_reasoning_message",
                    attributes={"message_id": rd.message_id},
                )
                self._logger.warn("reasoning message", state="dropped", reason="unfinished", message_id=rd.message_id)
                continue

            messages.append(
                orm.ReasoningMessage(
                    id=rd.message_id,
                    thread_id=self._thread_id,
                    created_at=rd.created_at,
                    content="".join(rd.content_deltas),
                )
            )

        for trd in self._tool_result_data.values():
            if trd.tool_call_id not in tool_calls:
                span = trace.get_current_span()
                span.add_event(
                    "orphaned_tool_message",
                    attributes={"message_id": trd.message_id, "tool_call_id": trd.tool_call_id},
                )
                self._logger.warn(
                    "tool message",
                    state="dropped",
                    reason="orphaned",
                    message_id=trd.message_id,
                    tool_call_id=trd.tool_call_id,
                )
                continue
            messages.append(
                orm.ToolMessage(
                    id=trd.message_id,
                    thread_id=self._thread_id,
                    created_at=trd.created_at,
                    content=trd.content,
                    tool_call=tool_calls[trd.tool_call_id],
                    # FIXME
                    error=None,
                    encrypted_value=None,
                )
            )

        return sorted(messages, key=lambda m: m.created_at)
