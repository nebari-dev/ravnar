import time
import uuid

import ag_ui.core
import compyre.api
import compyre.utils
import pydantic

from _ravnar import schema
from _ravnar.events import parse_timestamp


class EventProcessingCase(schema.BaseModel):
    thread_id: str = pydantic.Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = pydantic.Field(default_factory=lambda: str(uuid.uuid4()))
    parent_run_id: str | None = None
    state: ag_ui.core.State = None
    messages: list[ag_ui.core.Message] = pydantic.Field(default_factory=list)
    handle_run_lifecycle_events: bool = True
    input: list[ag_ui.core.Event]
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

    def case_activity_message_delta(self):
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

    def case_activity_message_snapshot(self):
        message_id = new_id()
        activity_type = "foo"
        first_content = {"baz": "qux", "foo": "bar"}
        second_content = {"replaced": True}
        first_timestamp = new_timestamp()
        second_timestamp = new_timestamp()

        event_stream = [
            ag_ui.core.ActivitySnapshotEvent(
                message_id=message_id,
                activity_type=activity_type,
                content=first_content,
                timestamp=first_timestamp,
            ),
            ag_ui.core.ActivitySnapshotEvent(
                message_id=message_id,
                activity_type=activity_type,
                content=second_content,
                replace=True,
                timestamp=second_timestamp,
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
                    content=second_content,
                    created_at=parse_timestamp(second_timestamp),
                )
            ],
        )

    def case_text_message(self):
        message_id = new_id()
        deltas = ["Hello, ", "world!"]
        timestamp = new_timestamp()

        event_stream = [
            ag_ui.core.TextMessageStartEvent(message_id=message_id, timestamp=timestamp),
            *[ag_ui.core.TextMessageContentEvent(message_id=message_id, delta=d) for d in deltas],
            ag_ui.core.TextMessageEndEvent(message_id=message_id),
        ]

        return EventProcessingCase(
            input=event_stream,
            expected_event_stream=event_stream,
            expected_state=None,
            expected_messages=[
                schema.AugmentedAssistantMessage(
                    id=message_id,
                    content="".join(deltas),
                    created_at=parse_timestamp(timestamp),
                    tool_calls=[],
                )
            ],
        )

    def case_state_snapshot(self):
        state = {"baz": "qux", "foo": "bar"}
        snapshot = {"baz": "boo", "hello": ["world"]}

        event_stream = [ag_ui.core.StateSnapshotEvent(snapshot=snapshot)]

        return EventProcessingCase(
            state=state,
            input=event_stream,
            expected_event_stream=event_stream,
            expected_state=snapshot,
            expected_messages=[],
        )

    def case_state_delta(self):
        state = {"baz": "qux", "foo": "bar"}
        event_stream = [
            ag_ui.core.StateDeltaEvent(
                delta=[
                    {"op": "replace", "path": "/baz", "value": "boo"},
                    {"op": "add", "path": "/hello", "value": ["world"]},
                ]
            ),
            ag_ui.core.StateDeltaEvent(delta=[{"op": "remove", "path": "/foo"}]),
        ]

        return EventProcessingCase(
            state=state,
            input=event_stream,
            expected_event_stream=event_stream,
            expected_state={"baz": "boo", "hello": ["world"]},
            expected_messages=[],
        )

    def case_tool_call_explicit_parent(self):
        parent_message_id = new_id()
        tool_call_id = new_id()
        tool_call_name = "test_tool"
        args_deltas = ['{"arg": ', '"value"}']
        result_message_id = new_id()
        result_content = "result"
        timestamp = new_timestamp()

        event_stream = [
            ag_ui.core.TextMessageStartEvent(message_id=parent_message_id, timestamp=timestamp),
            ag_ui.core.TextMessageEndEvent(message_id=parent_message_id),
            ag_ui.core.ToolCallStartEvent(
                tool_call_id=tool_call_id,
                tool_call_name=tool_call_name,
                parent_message_id=parent_message_id,
                timestamp=timestamp,
            ),
            *[ag_ui.core.ToolCallArgsEvent(tool_call_id=tool_call_id, delta=d) for d in args_deltas],
            ag_ui.core.ToolCallEndEvent(tool_call_id=tool_call_id),
            ag_ui.core.ToolCallResultEvent(
                message_id=result_message_id,
                tool_call_id=tool_call_id,
                content=result_content,
                timestamp=timestamp,
            ),
        ]

        return EventProcessingCase(
            input=event_stream,
            expected_event_stream=event_stream,
            expected_state=None,
            expected_messages=[
                schema.AugmentedAssistantMessage(
                    id=parent_message_id,
                    content=None,
                    created_at=parse_timestamp(timestamp),
                    tool_calls=[
                        ag_ui.core.ToolCall(
                            id=tool_call_id,
                            function=ag_ui.core.FunctionCall(
                                name=tool_call_name,
                                arguments="".join(args_deltas),
                            ),
                        )
                    ],
                ),
                schema.AugmentedToolMessage(
                    id=result_message_id,
                    content=result_content,
                    created_at=parse_timestamp(timestamp),
                    tool_call_id=tool_call_id,
                ),
            ],
        )

    def case_tool_call_implicit_parent(self):
        tool_call_id = new_id()
        tool_call_name = "test_tool"
        args_deltas = ['{"arg": ', '"value"}']
        result_message_id = new_id()
        result_content = "result"
        timestamp = new_timestamp()

        event_stream = [
            ag_ui.core.ToolCallStartEvent(
                tool_call_id=tool_call_id,
                tool_call_name=tool_call_name,
                parent_message_id=None,
                timestamp=timestamp,
            ),
            *[ag_ui.core.ToolCallArgsEvent(tool_call_id=tool_call_id, delta=d) for d in args_deltas],
            ag_ui.core.ToolCallEndEvent(tool_call_id=tool_call_id),
            ag_ui.core.ToolCallResultEvent(
                message_id=result_message_id,
                tool_call_id=tool_call_id,
                content=result_content,
                timestamp=timestamp,
            ),
        ]

        return EventProcessingCase(
            input=event_stream,
            expected_event_stream=event_stream,
            expected_state=None,
            expected_messages=[
                schema.AugmentedAssistantMessage(
                    id=new_generated_id(),
                    content=None,
                    created_at=parse_timestamp(timestamp),
                    tool_calls=[
                        ag_ui.core.ToolCall(
                            id=tool_call_id,
                            function=ag_ui.core.FunctionCall(
                                name=tool_call_name,
                                arguments="".join(args_deltas),
                            ),
                        )
                    ],
                ),
                schema.AugmentedToolMessage(
                    id=result_message_id,
                    content=result_content,
                    created_at=parse_timestamp(timestamp),
                    tool_call_id=tool_call_id,
                ),
            ],
        )

    def case_reasoning_message(self):
        message_id = new_id()
        deltas = ["reasoning ", "step"]
        timestamp = new_timestamp()

        event_stream = [
            ag_ui.core.ReasoningStartEvent(message_id=message_id),
            ag_ui.core.ReasoningMessageStartEvent(message_id=message_id, role="reasoning", timestamp=timestamp),
            *[ag_ui.core.ReasoningMessageContentEvent(message_id=message_id, delta=d) for d in deltas],
            ag_ui.core.ReasoningMessageEndEvent(message_id=message_id),
            ag_ui.core.ReasoningEndEvent(message_id=message_id),
        ]

        return EventProcessingCase(
            input=event_stream,
            expected_event_stream=event_stream,
            expected_state=None,
            expected_messages=[
                schema.AugmentedReasoningMessage(
                    id=message_id,
                    content="".join(deltas),
                    created_at=parse_timestamp(timestamp),
                )
            ],
        )

    def case_run_error(self):
        error_message = "something went wrong"
        error_code = "test:error"

        event_stream = [
            ag_ui.core.RunErrorEvent(message=error_message, code=error_code),
        ]

        return EventProcessingCase(
            input=event_stream,
            expected_event_stream=event_stream,
            expected_state=None,
            expected_messages=[],
        )

    def case_custom_event_passthrough(self):
        custom_name = "test"
        custom_value = {"foo": "bar"}

        event_stream = [
            ag_ui.core.CustomEvent(name=custom_name, value=custom_value),
        ]

        return EventProcessingCase(
            input=event_stream,
            expected_event_stream=event_stream,
            expected_state=None,
            expected_messages=[],
        )
