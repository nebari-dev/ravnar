import time
import uuid
from typing import Annotated

import ag_ui.core
import compyre.api
import compyre.utils
import pydantic

from _ravnar import schema
from _ravnar.events import parse_timestamp

AnyEvent = Annotated[
    ag_ui.core.TextMessageStartEvent
    | ag_ui.core.TextMessageContentEvent
    | ag_ui.core.TextMessageEndEvent
    | ag_ui.core.TextMessageChunkEvent
    | ag_ui.core.ToolCallStartEvent
    | ag_ui.core.ToolCallArgsEvent
    | ag_ui.core.ToolCallEndEvent
    | ag_ui.core.ToolCallChunkEvent
    | ag_ui.core.ToolCallResultEvent
    | ag_ui.core.StateSnapshotEvent
    | ag_ui.core.StateDeltaEvent
    | ag_ui.core.MessagesSnapshotEvent
    | ag_ui.core.ActivitySnapshotEvent
    | ag_ui.core.ActivityDeltaEvent
    | ag_ui.core.RawEvent
    | ag_ui.core.CustomEvent
    | ag_ui.core.RunStartedEvent
    | ag_ui.core.RunFinishedEvent
    | ag_ui.core.RunErrorEvent
    | ag_ui.core.StepStartedEvent
    | ag_ui.core.StepFinishedEvent
    | ag_ui.core.ReasoningStartEvent
    | ag_ui.core.ReasoningMessageStartEvent
    | ag_ui.core.ReasoningMessageContentEvent
    | ag_ui.core.ReasoningMessageEndEvent
    | ag_ui.core.ReasoningMessageChunkEvent
    | ag_ui.core.ReasoningEndEvent
    | ag_ui.core.ReasoningEncryptedValueEvent
    # the events below are deprecated and not part of ag_ui.core.Event
    | ag_ui.core.ThinkingStartEvent
    | ag_ui.core.ThinkingTextMessageStartEvent
    | ag_ui.core.ThinkingTextMessageContentEvent
    | ag_ui.core.ThinkingTextMessageEndEvent
    | ag_ui.core.ThinkingEndEvent,
    pydantic.Field(discriminator="type"),
]


class EventProcessingCase(schema.BaseModel):
    thread_id: str = pydantic.Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = pydantic.Field(default_factory=lambda: str(uuid.uuid4()))
    parent_run_id: str | None = None
    state: ag_ui.core.State = None
    messages: list[ag_ui.core.Message] = pydantic.Field(default_factory=list)
    handle_run_lifecycle_events: bool = True
    input: list[AnyEvent]
    expected_event_stream: list[ag_ui.core.Event]
    expected_state: ag_ui.core.State
    expected_messages: list[schema.AugmentedMessage]


def new_id() -> str:
    return str(uuid.uuid4())


def new_generated_id() -> str:
    return f"generated::{new_id()}"


def new_timestamp():
    return int(time.time_ns() / 1_000_000)


def pydantic_model_exclude_unpack_fn(p: compyre.api.Pair, /, *, exclude_none=False, exclude_generated_ids=True):
    if not compyre.utils.both_isinstance(p, pydantic.BaseModel):
        return None

    exclude = set()
    if exclude_generated_ids:
        id_fields = {f for f in type(p.expected).model_fields if f.endswith("id")}
        if missing := id_fields - set(type(p.actual).model_fields):
            return ValueError(
                f"The following ID fields are present in the expected model, but not in the actual: {sorted(missing)}"
            )

        exclude.update(
            f
            for f in id_fields
            if isinstance(v := getattr(p.expected, f), str)
            and v.startswith("generated::")
            and isinstance(getattr(p.actual, f), str)
        )

    try:
        actual = p.actual.model_dump(exclude=exclude, exclude_none=exclude_none)
        expected = p.expected.model_dump(exclude=exclude, exclude_none=exclude_none)
    except Exception as result:
        return result

    return compyre.builtin.unpack_fns.collections_mapping(
        compyre.api.Pair(index=p.index, actual=actual, expected=expected)
    )


class EventProcessingCases:
    def case_thinking_to_reasoning_conversion(self):
        message_id = new_generated_id()
        deltas = ["thinking", "more"]

        timestamp = new_timestamp()

        return EventProcessingCase(
            input=[
                ag_ui.core.ThinkingStartEvent(),
                ag_ui.core.ThinkingTextMessageStartEvent(timestamp=timestamp),
                *[ag_ui.core.ThinkingTextMessageContentEvent(delta=d) for d in deltas],
                ag_ui.core.ThinkingTextMessageEndEvent(),
                ag_ui.core.ThinkingEndEvent(),
            ],
            expected_event_stream=[
                ag_ui.core.ReasoningStartEvent(message_id=message_id),
                ag_ui.core.ReasoningMessageStartEvent(message_id=message_id, role="reasoning", timestamp=timestamp),
                *[ag_ui.core.ReasoningMessageContentEvent(message_id=message_id, delta=d) for d in deltas],
                ag_ui.core.ReasoningMessageEndEvent(message_id=message_id),
                ag_ui.core.ReasoningEndEvent(message_id=message_id),
            ],
            expected_state=None,
            expected_messages=[
                schema.AugmentedReasoningMessage(
                    id=message_id, content="".join(deltas), created_at=parse_timestamp(timestamp)
                )
            ],
        )

    def case_activity_message(self):
        message_id = new_id()
        activity_type = "foo"

        snapshot_timestamp = new_timestamp()
        last_patch_timestamp = new_timestamp()

        event_stream = [
            ag_ui.core.ActivitySnapshotEvent(
                message_id=message_id,
                activity_type=activity_type,
                content={"baz": "qux", "foo": "bar"},
                timestamp=snapshot_timestamp,
            ),
            ag_ui.core.ActivityDeltaEvent(
                message_id=message_id,
                activity_type=activity_type,
                patch=[
                    {"op": "replace", "path": "/baz", "value": "boo"},
                    {"op": "add", "path": "/hello", "value": ["world"]},
                ],
            ),
            ag_ui.core.ActivityDeltaEvent(
                message_id=message_id,
                activity_type=activity_type,
                patch=[{"op": "remove", "path": "/foo"}],
                timestamp=last_patch_timestamp,
            ),
        ]

        return EventProcessingCase(
            input=event_stream,
            expected_event_stream=event_stream,
            expected_state=None,
            expected_messages=[
                schema.AugmentedActivityMessage(
                    id=message_id,
                    activity_type=activity_type,
                    content={"baz": "boo", "hello": ["world"]},
                    created_at=parse_timestamp(snapshot_timestamp),
                    updated_at=parse_timestamp(last_patch_timestamp),
                )
            ],
        )
