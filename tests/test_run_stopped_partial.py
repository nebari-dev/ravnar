"""A user-stopped run must keep its partial (unfinished) answer.

Best-practice Stop behavior: when the user stops generation mid-stream, the
partial answer stays in the conversation (marked stopped) and feeds the next
turn -- stopping ends a turn early, it does not erase it.

`_extract_messages` normally drops unfinished text messages. `mark_stopped()`
(called by the run's persist-on-cancel path in `core.py`) flips that so the
partial assistant message is preserved on extract.
"""

import ag_ui.core
import pytest

from _ravnar.events import EventProcessor
from _ravnar.utils import as_async_iterator

PARTIAL = "Once upon a time in the swamp there lived"
MESSAGE_ID = "assistant-partial"


def _run_input() -> ag_ui.core.RunAgentInput:
    return ag_ui.core.RunAgentInput(
        thread_id="t-1",
        run_id="r-1",
        parent_run_id=None,
        state=None,
        messages=[],
        tools=[],
        context=[],
        forwarded_props=None,
    )


def _partial_stream() -> list[ag_ui.core.Event]:
    # RunStarted + a text message that starts and streams content but never ends
    # (the run is interrupted mid-generation). No RunFinished.
    return [
        ag_ui.core.RunStartedEvent(thread_id="t-1", run_id="r-1"),
        ag_ui.core.TextMessageStartEvent(message_id=MESSAGE_ID),
        *[
            ag_ui.core.TextMessageContentEvent(message_id=MESSAGE_ID, delta=word + " ")
            for word in PARTIAL.split()
        ],
    ]


async def _process(processor: EventProcessor) -> None:
    async for _ in processor.process_event_stream(
        as_async_iterator(iter, _partial_stream())
    ):
        pass


@pytest.mark.asyncio
async def test_stopped_run_keeps_partial_assistant_message():
    from _ravnar import orm

    processor = EventProcessor(run_agent_input=_run_input())
    await _process(processor)

    # The user stopped the run: keep the partial answer.
    processor.mark_stopped()
    run = processor.extract(include_input_message_ids=set())

    contents = [
        m.content for m in run.messages if isinstance(m, orm.AssistantMessage)
    ]
    assert any(c and PARTIAL.split()[0] in c for c in contents), (
        "a stopped run must keep its partial answer so the interrupted response "
        f"survives in history; got assistant contents={contents!r}"
    )


@pytest.mark.asyncio
async def test_unstopped_run_drops_unfinished_message():
    from _ravnar import orm

    processor = EventProcessor(run_agent_input=_run_input())
    await _process(processor)

    # Not stopped: the unfinished text message is dropped as before (no
    # behavioral change for normal runs).
    run = processor.extract(include_input_message_ids=set())

    contents = [
        m.content for m in run.messages if isinstance(m, orm.AssistantMessage)
    ]
    assert not any(c and PARTIAL.split()[0] in c for c in contents), (
        "an unfinished (non-stopped) message must still be dropped; "
        f"got assistant contents={contents!r}"
    )
