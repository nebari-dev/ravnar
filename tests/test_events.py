import ag_ui.core
import compyre
import compyre.api
import pydantic
import pytest_cases

from _ravnar import schema
from _ravnar.events import EventProcessor
from _ravnar.utils import as_async_iterator

from . import test_events_cases


class TestEventProcessor:
    def assert_equal(self, actual, expected):
        __tracebackhide__ = True
        compyre.api.assert_equal(
            actual,
            expected,
            unpack_fns=[test_events_cases.pydantic_model_exclude_unpack_fn, *compyre.default_unpack_fns()],
            equal_fns=compyre.default_equal_fns(),
            exclude_none=True,
        )

    @pytest_cases.parametrize_with_cases("test_case", cases=test_events_cases.EventProcessingCases)
    async def test_event_processing(self, test_case: test_events_cases.EventProcessingCase):
        event_processor = EventProcessor(
            thread_id=test_case.thread_id,
            run_id=test_case.run_id,
            parent_run_id=test_case.parent_run_id,
            state=test_case.state,
            messages=test_case.messages,
        )

        input = test_case.input
        if test_case.handle_run_lifecycle_events:
            input = [
                ag_ui.core.RunStartedEvent(
                    thread_id=test_case.thread_id, run_id=test_case.run_id, parent_run_id=test_case.parent_run_id
                ),
                *input,
                ag_ui.core.RunFinishedEvent(thread_id=test_case.thread_id, run_id=test_case.run_id),
            ]

        actual_event_stream = [e async for e in event_processor.process_event_stream(as_async_iterator(iter, input))]
        if test_case.handle_run_lifecycle_events:
            actual_event_stream = [
                e
                for e in actual_event_stream
                if not isinstance(e, (ag_ui.core.RunStartedEvent, ag_ui.core.RunFinishedEvent))
            ]

        self.assert_equal(actual_event_stream, test_case.expected_event_stream)

        actual_state, actual_orm_messages = event_processor.extract()
        actual_messages = pydantic.TypeAdapter(list[schema.AugmentedMessage]).validate_python(
            actual_orm_messages, from_attributes=True
        )

        compyre.assert_equal(actual_state, test_case.expected_state)
        self.assert_equal(actual_messages, test_case.expected_messages)
