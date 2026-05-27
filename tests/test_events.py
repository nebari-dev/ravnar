import ag_ui.core
import compyre
import compyre.api
import compyre.utils
import pytest_cases

from _ravnar.events import EventProcessor
from _ravnar.utils import as_async_iterator

from . import test_events_cases


class TestEventProcessor:
    def assert_equal(self, actual, expected):
        __tracebackhide__ = True
        compyre.api.assert_equal(
            actual,
            expected,
            unpack_fns=[
                test_events_cases.pydantic_model_exclude_unpack_fn,
                test_events_cases.orm_exclude_unpack_fn,
                *compyre.default_unpack_fns(),
            ],
            equal_fns=compyre.default_equal_fns(),
            exclude_none=True,
            exclude_sentinels=True,
        )

    @pytest_cases.parametrize_with_cases("test_case", cases=test_events_cases.EventProcessingCases)
    async def test_event_processing(self, test_case: test_events_cases.EventProcessingCase):
        event_processor = EventProcessor(run_agent_input=test_case.run_agent_input)

        agent_event_stream = test_case.agent_event_stream
        if test_case.handle_run_lifecycle_events:
            agent_event_stream = [
                ag_ui.core.RunStartedEvent(
                    thread_id=test_case.run_agent_input.thread_id,
                    run_id=test_case.run_agent_input.run_id,
                    parent_run_id=test_case.run_agent_input.parent_run_id,
                ),
                *agent_event_stream,
                ag_ui.core.RunFinishedEvent(
                    thread_id=test_case.run_agent_input.thread_id, run_id=test_case.run_agent_input.run_id
                ),
            ]

        actual_event_stream = [
            e async for e in event_processor.process_event_stream(as_async_iterator(iter, agent_event_stream))
        ]
        if test_case.handle_run_lifecycle_events:
            actual_event_stream = [
                e
                for e in actual_event_stream
                if not isinstance(e, (ag_ui.core.RunStartedEvent, ag_ui.core.RunFinishedEvent))
            ]

        # self.assert_equal(actual_event_stream, test_case.expected_event_stream)

        actual_run = event_processor.extract(
            include_input_message_ids={m.id for m in test_case.run_agent_input.messages}
        )
        self.assert_equal(actual_run, test_case.expected_run)
