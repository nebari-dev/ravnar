import dataclasses
import json
import time
import uuid
from datetime import datetime

import ag_ui.core
import compyre.api
import compyre.utils
import pydantic

from _ravnar import orm, schema
from _ravnar.events import parse_timestamp as parse_event_timestamp
from _ravnar.utils import now
from tests.utils import Sentinels


def _compute_excluded_sentinel_fields(p, /, *, fields_fn):
    exclude = set()

    actual = {f: getattr(p.actual, f) for f in fields_fn(p.actual)}
    expected = {f: getattr(p.expected, f) for f in fields_fn(p.expected)}

    uuids = {f: v for f, v in expected.items() if isinstance(v, uuid.UUID)}
    if missing := uuids.keys() - actual.keys():
        raise ValueError(
            f"The following UUID fields are present in the expected model, but not in the actual: {sorted(missing)}"
        )

    exclude.update(f for f, v in uuids.items() if Sentinels.is_uuid_sentinel(v) and isinstance(actual[f], uuid.UUID))

    ids = {f: v for f, v in expected.items() if f.endswith("id")}
    if missing := ids.keys() - actual.keys():
        raise ValueError(
            f"The following ID fields are present in the expected model, but not in the actual: {sorted(missing)}"
        )

    exclude.update(
        f for f, v in ids.items() if isinstance(v, str) and Sentinels.is_id_sentinel(v) and isinstance(actual[f], str)
    )

    datetimes = {f: v for f, v in expected.items() if isinstance(v, datetime)}
    if missing := datetimes.keys() - actual.keys():
        raise ValueError(
            f"The following datetime fields are present in the expected model, but not in the actual: {sorted(missing)}"
        )

    exclude.update(
        f for f, v in datetimes.items() if Sentinels.is_datetime_sentinel(v) and isinstance(actual[f], datetime)
    )

    return exclude


def pydantic_model_exclude_unpack_fn(
    p: compyre.api.Pair,
    /,
    *,
    exclude_none=False,
    exclude_sentinels=False,
):
    if not compyre.utils.both_isinstance(p, pydantic.BaseModel):
        return None

    if not isinstance(p.actual, type(p.expected)):
        return TypeError(f"Type mismatch: {type(p.actual)} != {type(p.expected)}")

    if exclude_sentinels:
        try:
            exclude = _compute_excluded_sentinel_fields(p, fields_fn=lambda pm: type(pm).model_fields)
        except Exception as result:
            return result
    else:
        exclude = set()

    try:
        actual = p.actual.model_dump(exclude=exclude, exclude_none=exclude_none)
        expected = p.expected.model_dump(exclude=exclude, exclude_none=exclude_none)
    except Exception as result:
        return result

    return compyre.builtin.unpack_fns.collections_mapping(
        compyre.api.Pair(index=p.index, actual=actual, expected=expected)
    )


def orm_exclude_unpack_fn(
    p: compyre.api.Pair,
    /,
    *,
    exclude_none=False,
    exclude_sentinels=False,
):
    if not compyre.utils.both_isinstance(p, orm.Base):
        return None

    if not isinstance(p.actual, type(p.expected)):
        return TypeError(f"Type mismatch: {type(p.actual)} != {type(p.expected)}")

    def fields_fn(o):
        return {f.name for f in dataclasses.fields(o)}

    if exclude_sentinels:
        try:
            exclude = _compute_excluded_sentinel_fields(p, fields_fn=fields_fn)
        except Exception as result:
            return result
    else:
        exclude = set()

    def as_dict(o):
        # We cannot use dataclasses.as_dict here, because it recurses through the whole object, but does not handle
        # reference cycles. A shallow dict is fine, because compyre will keep unpacking and has cycle detection built
        # in.
        dct = {}
        for f in fields_fn(o):
            v = getattr(o, f)
            if exclude_none and v is None:
                continue
            if f in exclude:
                continue
            dct[f] = v
        return dct

    return compyre.builtin.unpack_fns.collections_mapping(
        compyre.api.Pair(index=p.index, actual=as_dict(p.actual), expected=as_dict(p.expected))
    )


def new_uid() -> uuid.UUID:
    return uuid.uuid4()


def new_id() -> str:
    return str(uuid.uuid4())


def new_timestamp():
    return now()


def new_event_timestamp():
    return int(time.time_ns() / 1_000_000)


@dataclasses.dataclass(kw_only=True, frozen=True)
class EventProcessingCase:
    parent_state: orm.State = None
    parent_messages: list[orm.Message] = dataclasses.field(default_factory=list)
    thread_id: str = dataclasses.field(default_factory=new_id)
    create_run_data: schema.CreateRunData
    handle_run_lifecycle_events: bool = True
    agent_event_stream: list[ag_ui.core.Event]
    expected_event_stream: list[ag_ui.core.Event]
    expected_run: orm.Run


class EventProcessingCases:
    def case_thinking_to_reasoning_conversion(self, sentinels):
        create_run_data = schema.CreateRunData(messages=[])

        message_id = sentinels.new_id()
        deltas = ["thinking", "more"]

        timestamp = new_event_timestamp()

        return EventProcessingCase(
            create_run_data=create_run_data,
            agent_event_stream=[
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
            expected_run=orm.Run(
                id=create_run_data.id,
                thread_id=sentinels.new_id(),
                parent_run_id=create_run_data.parent_run_id,
                created_at=sentinels.new_datetime(),
                messages=[
                    orm.ReasoningMessage(
                        uid=sentinels.new_uuid(),
                        run_id=create_run_data.id,
                        id=message_id,
                        created_at=parse_event_timestamp(timestamp),
                        content="".join(deltas),
                    )
                ],
            ),
        )

    def case_activity_message_delta(self, sentinels):
        create_run_data = schema.CreateRunData(messages=[])

        message_id = new_id()
        activity_type = "foo"

        snapshot_timestamp = new_event_timestamp()
        last_patch_timestamp = new_event_timestamp()

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
            create_run_data=create_run_data,
            agent_event_stream=event_stream,
            expected_event_stream=event_stream,
            expected_run=orm.Run(
                id=create_run_data.id,
                thread_id=sentinels.new_id(),
                parent_run_id=create_run_data.parent_run_id,
                created_at=sentinels.new_datetime(),
                messages=[
                    orm.ActivityMessage(
                        uid=sentinels.new_uuid(),
                        run_id=create_run_data.id,
                        id=message_id,
                        created_at=parse_event_timestamp(snapshot_timestamp),
                        content={"baz": "boo", "hello": ["world"]},
                        activity_type=activity_type,
                    )
                ],
            ),
        )

    def case_activity_message_snapshot(self, sentinels):
        create_run_data = schema.CreateRunData(messages=[])

        message_id = new_id()
        activity_type = "foo"
        first_content = {"baz": "qux", "foo": "bar"}
        second_content = {"replaced": True}
        first_timestamp = new_event_timestamp()
        second_timestamp = new_event_timestamp()

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
            create_run_data=create_run_data,
            agent_event_stream=event_stream,
            expected_event_stream=event_stream,
            expected_run=orm.Run(
                id=create_run_data.id,
                thread_id=sentinels.new_id(),
                parent_run_id=create_run_data.parent_run_id,
                created_at=sentinels.new_datetime(),
                messages=[
                    orm.ActivityMessage(
                        uid=sentinels.new_uuid(),
                        run_id=create_run_data.id,
                        id=message_id,
                        created_at=parse_event_timestamp(second_timestamp),
                        content=second_content,
                        activity_type=activity_type,
                    )
                ],
            ),
        )

    def case_text_message(self, sentinels):
        create_run_data = schema.CreateRunData(messages=[])

        message_id = new_id()
        deltas = ["Hello, ", "world!"]
        timestamp = new_event_timestamp()

        event_stream = [
            ag_ui.core.TextMessageStartEvent(message_id=message_id, timestamp=timestamp),
            *[ag_ui.core.TextMessageContentEvent(message_id=message_id, delta=d) for d in deltas],
            ag_ui.core.TextMessageEndEvent(message_id=message_id),
        ]

        return EventProcessingCase(
            create_run_data=create_run_data,
            agent_event_stream=event_stream,
            expected_event_stream=event_stream,
            expected_run=orm.Run(
                id=create_run_data.id,
                thread_id=sentinels.new_id(),
                parent_run_id=create_run_data.parent_run_id,
                created_at=sentinels.new_datetime(),
                messages=[
                    orm.AssistantMessage(
                        uid=sentinels.new_uuid(),
                        run_id=create_run_data.id,
                        id=message_id,
                        created_at=parse_event_timestamp(timestamp),
                        content="".join(deltas),
                        tool_calls=[],
                    )
                ],
            ),
        )

    def case_state_snapshot(self, sentinels):
        create_run_data = schema.CreateRunData(messages=[])

        state = {"baz": "qux", "foo": "bar"}
        snapshot = {"baz": "boo", "hello": ["world"]}

        event_stream = [ag_ui.core.StateSnapshotEvent(snapshot=snapshot)]

        return EventProcessingCase(
            parent_state=state,
            create_run_data=create_run_data,
            agent_event_stream=event_stream,
            expected_event_stream=event_stream,
            expected_run=orm.Run(
                id=create_run_data.id,
                thread_id=sentinels.new_id(),
                parent_run_id=create_run_data.parent_run_id,
                created_at=sentinels.new_datetime(),
                messages=[],
                state=snapshot,
            ),
        )

    def case_state_delta(self, sentinels):
        create_run_data = schema.CreateRunData(messages=[])

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
            parent_state=state,
            create_run_data=create_run_data,
            agent_event_stream=event_stream,
            expected_event_stream=event_stream,
            expected_run=orm.Run(
                id=create_run_data.id,
                thread_id=sentinels.new_id(),
                parent_run_id=create_run_data.parent_run_id,
                created_at=sentinels.new_datetime(),
                messages=[],
                state={"baz": "boo", "hello": ["world"]},
            ),
        )

    def case_tool_call_explicit_parent(self, sentinels):
        create_run_data = schema.CreateRunData(messages=[])

        parent_message_id = new_id()
        tool_call_id = new_id()
        tool_call_name = "test_tool"
        args_deltas = ['{"arg": ', '"value"}']
        result_message_id = new_id()
        result_content = "result"
        timestamp = new_event_timestamp()

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

        assistant_message_uid = sentinels.new_uuid()
        tool_message_uid = sentinels.new_uuid()
        tool_call = orm.ToolCall(
            uid=assistant_message_uid,
            id=tool_call_id,
            name=tool_call_name,
            arguments="".join(args_deltas),
            assistant_message_uid=assistant_message_uid,
            tool_message_uid=tool_message_uid,
        )

        return EventProcessingCase(
            create_run_data=create_run_data,
            agent_event_stream=event_stream,
            expected_event_stream=event_stream,
            expected_run=orm.Run(
                id=create_run_data.id,
                thread_id=sentinels.new_id(),
                parent_run_id=create_run_data.parent_run_id,
                created_at=sentinels.new_datetime(),
                messages=[
                    orm.AssistantMessage(
                        uid=assistant_message_uid,
                        run_id=create_run_data.id,
                        id=parent_message_id,
                        created_at=parse_event_timestamp(timestamp),
                        content=None,
                        tool_calls=[tool_call],
                    ),
                    orm.ToolMessage(
                        uid=tool_message_uid,
                        run_id=create_run_data.id,
                        id=result_message_id,
                        content=result_content,
                        created_at=parse_event_timestamp(timestamp),
                        tool_call=tool_call,
                    ),
                ],
            ),
        )

    def case_tool_call_implicit_parent(self, sentinels):
        create_run_data = schema.CreateRunData(messages=[])

        tool_call_id = new_id()
        tool_call_name = "test_tool"
        args_deltas = ['{"arg": ', '"value"}']
        result_message_id = new_id()
        result_content = "result"
        timestamp = new_event_timestamp()

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

        assistant_message_uid = sentinels.new_uuid()
        tool_message_uid = sentinels.new_uuid()
        tool_call = orm.ToolCall(
            uid=assistant_message_uid,
            id=tool_call_id,
            name=tool_call_name,
            arguments="".join(args_deltas),
            assistant_message_uid=assistant_message_uid,
            tool_message_uid=tool_message_uid,
        )

        return EventProcessingCase(
            create_run_data=create_run_data,
            agent_event_stream=event_stream,
            expected_event_stream=event_stream,
            expected_run=orm.Run(
                id=create_run_data.id,
                thread_id=sentinels.new_id(),
                parent_run_id=create_run_data.parent_run_id,
                created_at=sentinels.new_datetime(),
                messages=[
                    orm.AssistantMessage(
                        uid=assistant_message_uid,
                        run_id=create_run_data.id,
                        id=sentinels.new_id(),
                        created_at=parse_event_timestamp(timestamp),
                        content=None,
                        tool_calls=[tool_call],
                    ),
                    orm.ToolMessage(
                        uid=tool_message_uid,
                        run_id=create_run_data.id,
                        id=result_message_id,
                        content=result_content,
                        created_at=parse_event_timestamp(timestamp),
                        tool_call=tool_call,
                    ),
                ],
            ),
        )

    def case_reasoning_message(self, sentinels):
        create_run_data = schema.CreateRunData(messages=[])

        message_id = new_id()
        deltas = ["reasoning ", "step"]
        timestamp = new_event_timestamp()

        event_stream = [
            ag_ui.core.ReasoningStartEvent(message_id=message_id),
            ag_ui.core.ReasoningMessageStartEvent(message_id=message_id, role="reasoning", timestamp=timestamp),
            *[ag_ui.core.ReasoningMessageContentEvent(message_id=message_id, delta=d) for d in deltas],
            ag_ui.core.ReasoningMessageEndEvent(message_id=message_id),
            ag_ui.core.ReasoningEndEvent(message_id=message_id),
        ]

        return EventProcessingCase(
            create_run_data=create_run_data,
            agent_event_stream=event_stream,
            expected_event_stream=event_stream,
            expected_run=orm.Run(
                id=create_run_data.id,
                thread_id=sentinels.new_id(),
                parent_run_id=create_run_data.parent_run_id,
                created_at=sentinels.new_datetime(),
                messages=[
                    orm.ReasoningMessage(
                        uid=sentinels.new_uuid(),
                        run_id=create_run_data.id,
                        id=message_id,
                        created_at=parse_event_timestamp(timestamp),
                        content="".join(deltas),
                    )
                ],
            ),
        )

    def case_run_error(self, sentinels):
        create_run_data = schema.CreateRunData(messages=[])

        error_message = "something went wrong"
        error_code = "test:error"

        event_stream = [
            ag_ui.core.RunErrorEvent(message=error_message, code=error_code),
        ]

        return EventProcessingCase(
            create_run_data=create_run_data,
            agent_event_stream=event_stream,
            expected_event_stream=event_stream,
            expected_run=orm.Run(
                id=create_run_data.id,
                thread_id=sentinels.new_id(),
                parent_run_id=create_run_data.parent_run_id,
                created_at=sentinels.new_datetime(),
                messages=[],
            ),
        )

    def case_custom_event_passthrough(self, sentinels):
        create_run_data = schema.CreateRunData(messages=[])

        custom_name = "test"
        custom_value = {"foo": "bar"}

        event_stream = [
            ag_ui.core.CustomEvent(name=custom_name, value=custom_value),
        ]

        return EventProcessingCase(
            create_run_data=create_run_data,
            agent_event_stream=event_stream,
            expected_event_stream=event_stream,
            expected_run=orm.Run(
                id=create_run_data.id,
                thread_id=sentinels.new_id(),
                parent_run_id=create_run_data.parent_run_id,
                created_at=sentinels.new_datetime(),
                messages=[],
            ),
        )

    def case_frontend_tool_call(self, sentinels):
        assistant_message_id = new_id()
        assistant_message_timestamp = new_timestamp()

        tool_call_id = new_id()
        tool_call_name = "fetch_weather"
        tool_call_args = json.dumps({"city": "Reykjavik"})

        tool_message_id = new_id()
        tool_message_content = json.dumps({"temp": 2, "unit": "C"})
        tool_message_timestamp = new_timestamp()

        parent_messages = [
            orm.AssistantMessage(
                id=assistant_message_id,
                run_id=new_id(),
                created_at=assistant_message_timestamp,
                content=None,
                tool_calls=[
                    orm.ToolCall(
                        uid=new_uid(),
                        id=tool_call_id,
                        name=tool_call_name,
                        arguments=tool_call_args,
                        assistant_message_uid=new_uid(),
                        tool_message_uid=None,
                    )
                ],
            )
        ]

        create_run_data = schema.CreateRunData(
            messages=[
                schema.AugmentedToolMessage(
                    id=tool_message_id,
                    content=tool_message_content,
                    tool_call_id=tool_call_id,
                    created_at=tool_message_timestamp,
                )
            ]
        )

        event_stream = []

        expected_assistant_message_uid = sentinels.new_uuid()
        expected_tool_message_uid = sentinels.new_uuid()

        expected_tool_call = orm.ToolCall(
            uid=sentinels.new_uuid(),
            id=tool_call_id,
            name=tool_call_name,
            arguments=tool_call_args,
            assistant_message_uid=expected_assistant_message_uid,
            tool_message_uid=expected_tool_message_uid,
        )
        expected_assistant_message = orm.AssistantMessage(
            uid=expected_assistant_message_uid,
            run_id=create_run_data.id,
            id=assistant_message_id,
            created_at=assistant_message_timestamp,
            content=None,
            tool_calls=[],
        )
        expected_tool_message = orm.ToolMessage(
            uid=expected_tool_message_uid,
            run_id=create_run_data.id,
            id=tool_message_id,
            content=tool_message_content,
            created_at=tool_message_timestamp,
            tool_call=expected_tool_call,
        )

        expected_tool_call.assistant_message = expected_assistant_message
        expected_tool_call.tool_message = expected_tool_message

        return EventProcessingCase(
            parent_messages=parent_messages,
            create_run_data=create_run_data,
            agent_event_stream=event_stream,
            expected_event_stream=event_stream,
            expected_run=orm.Run(
                id=create_run_data.id,
                thread_id=sentinels.new_id(),
                parent_run_id=create_run_data.parent_run_id,
                created_at=sentinels.new_datetime(),
                messages=[expected_tool_message],
            ),
        )
