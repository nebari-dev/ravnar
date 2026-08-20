"""A client disconnect mid-run must persist the stopped turn.

This covers the actual boundary: client disconnect -> the run's `event_stream`
catches the cancellation -> `mark_stopped()` + the persist callback runs
(shielded) -> the partial turn is saved. It complements
`test_run_stopped_partial.py`, which only checks that `extract()` keeps a partial
message once `mark_stopped()` has been called.
"""

import asyncio
import contextlib

import ag_ui.core

from _ravnar import orm
from _ravnar.agents import DefaultAgent
from _ravnar.config import AgentConfig
from _ravnar.core import AgentHandler
from _ravnar.schema.api import AugmentedRunAgentInput
from _ravnar.security import User
from _ravnar.utils import ImportStringWithParams

PARTIAL = "Once upon a time in the swamp there lived"
MESSAGE_ID = "assistant-partial"


class PartialThenBlockAgent(DefaultAgent):
    """Streams a partial (unfinished) assistant message, then blocks forever.

    The only way this run ends is a cancellation (client disconnect).
    """

    async def run(self, input: ag_ui.core.RunAgentInput, user: User):
        yield ag_ui.core.RunStartedEvent(thread_id=input.thread_id, run_id=input.run_id)
        yield ag_ui.core.TextMessageStartEvent(message_id=MESSAGE_ID)
        for word in PARTIAL.split():
            yield ag_ui.core.TextMessageContentEvent(message_id=MESSAGE_ID, delta=word + " ")
        await asyncio.Event().wait()


def _run_input() -> AugmentedRunAgentInput:
    return AugmentedRunAgentInput(
        thread_id="t-1",
        run_id="r-1",
        parent_run_id=None,
        state=None,
        messages=[],
        tools=[],
        context=[],
        forwarded_props=None,
    )


class TestPersistOnCancel:
    async def test_client_disconnect_persists_stopped_partial(self):
        handler = AgentHandler(
            AgentConfig(static={"a": ImportStringWithParams(cls_or_fn=PartialThenBlockAgent)})
        )

        captured: dict[str, orm.Run] = {}

        async def callback(event_processor) -> None:
            captured["run"] = event_processor.extract(include_input_message_ids=set())

        response = await handler.run("a", _run_input(), user=User.default(), callback=callback)
        stream = response.body_iterator

        seen: list[bytes] = []

        async def consume() -> None:
            async for chunk in stream:
                seen.append(bytes(chunk))

        def _content_chunks() -> int:
            return sum(1 for c in seen if b"TEXT_MESSAGE_CONTENT" in c)

        task = asyncio.create_task(consume())
        for _ in range(500):
            if _content_chunks() >= 4:
                break
            await asyncio.sleep(0.01)
        assert _content_chunks() >= 4, "partial never streamed"

        # Cancel the consuming task exactly as uvicorn cancels the response task
        # when the client disconnects.
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # Let the shielded persist callback complete.
        await asyncio.sleep(0)

        assert "run" in captured, (
            "the persist callback did not run on a client-disconnect cancellation "
            "-- the stopped turn would be lost"
        )
        contents = [
            m.content for m in captured["run"].messages if isinstance(m, orm.AssistantMessage)
        ]
        assert any(c and PARTIAL.split()[0] in c for c in contents), (
            "the stopped run persisted no partial answer; got assistant "
            f"contents={contents!r}"
        )
