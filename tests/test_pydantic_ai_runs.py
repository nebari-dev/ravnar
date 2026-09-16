"""Exercise Pydantic AI through Ravnar, without provider credentials or network calls."""

import asyncio
import copy
import uuid
from collections.abc import AsyncIterator

import ag_ui.core as ag
import httpx_sse
import pydantic
import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai_harness.compaction import ClearToolResults

from _ravnar import schema
from _ravnar.agents import PydanticAiAgentWrapper
from _ravnar.config import BaseConfig, DatabaseConfig
from _ravnar.core import AgentHandler
from _ravnar.database import Database
from _ravnar.events import EventProcessor
from _ravnar.security import User
from tests.utils import HeaderAuthenticator, TestClient, make_app_client


def run_thread(client: TestClient, thread_id: str, run_id: str) -> list[ag.Event]:
    with httpx_sse.connect_sse(
        client,
        "POST",
        f"/api/threads/{thread_id}/runs",
        json={"id": run_id, "messages": [{"role": "user", "content": [{"type": "text", "text": "Look up data"}]}]},
    ) as source:
        source.response.raise_for_status()
        return [pydantic.TypeAdapter(ag.Event).validate_json(sse.data) for sse in source.iter_sse()]


@pytest.mark.parametrize("compact", [False, True])
def test_tools_compaction_and_persisted_continuation(compact: bool) -> None:
    requests: list[list[ModelMessage]] = []
    tool_users: list[str] = []
    payload = "Large tool result. " * 1000

    async def model(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(copy.deepcopy(messages))
        if any(isinstance(part, ToolReturnPart) for part in messages[-1].parts):
            yield "Finished."
        else:
            yield {0: DeltaToolCall(name="lookup", json_args="{}", tool_call_id=str(uuid.uuid4()))}

    agent = Agent(
        FunctionModel(stream_function=model),
        deps_type=User,
        capabilities=[ClearToolResults(max_messages=1, keep_pairs=0)] if compact else [],
    )

    @agent.tool
    def lookup(ctx: RunContext[User]) -> str:
        tool_users.append(ctx.deps.id)
        return payload

    wrapper = PydanticAiAgentWrapper(agent)
    config = BaseConfig.model_validate(
        {"agents": {"static": {"test": lambda: wrapper}}, "security": {"authenticator": HeaderAuthenticator}}
    )
    with make_app_client(config) as client:
        thread_id = client.post("/api/threads", json={"agentId": "test"}).raise_for_status().json()["id"]
        for run_id in ("first", "continued"):
            events = run_thread(client, thread_id, run_id)
            assert isinstance(events[0], ag.RunStartedEvent)
            assert isinstance(events[-1], ag.RunFinishedEvent)
            assert not any(isinstance(event, ag.RunErrorEvent) for event in events)
            assert sum(isinstance(event, ag.ToolCallStartEvent) for event in events) == 1
            assert sum(isinstance(event, ag.ToolCallResultEvent) for event in events) == 1
            assert (
                "".join(event.delta for event in events if isinstance(event, ag.TextMessageContentEvent)) == "Finished."
            )

        saved = pydantic.TypeAdapter(list[schema.AugmentedMessage]).validate_python(
            client.get(f"/api/threads/{thread_id}/messages").raise_for_status().json()
        )
        # Ravnar keeps the original transcript. The configured capability edits
        # model-facing history again when the saved conversation is continued.
        results = [message for message in saved if isinstance(message, ag.ToolMessage)]
        assert len(results) == 2
        assert all(message.content == payload for message in results)
        calls = {
            call.id
            for message in saved
            if isinstance(message, ag.AssistantMessage)
            for call in message.tool_calls or []
        }
        assert calls == {message.tool_call_id for message in results}

    assert tool_users == ["pytest", "pytest"]  # No replay of a previous turn's tool.
    assert len(requests) == 4
    for messages in requests:
        call_ids = {
            part.tool_call_id for message in messages for part in message.parts if isinstance(part, ToolCallPart)
        }
        returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        assert call_ids == {part.tool_call_id for part in returns}
        for part in returns:
            assert part.content == ("[tool result cleared]" if compact else payload)


def test_model_failure_is_a_terminal_ag_ui_error() -> None:
    async def model(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        raise RuntimeError("model unavailable")
        yield  # pragma: no cover

    wrapper = PydanticAiAgentWrapper(Agent(FunctionModel(stream_function=model)))
    config = BaseConfig.model_validate({"agents": {"static": {"test": lambda: wrapper}}})
    with make_app_client(config) as client:
        thread_id = client.post("/api/threads", json={"agentId": "test"}).raise_for_status().json()["id"]
        events = run_thread(client, thread_id, "failed")
        assert isinstance(events[0], ag.RunStartedEvent)
        assert isinstance(events[-1], ag.RunErrorEvent)
        assert sum(isinstance(event, ag.RunErrorEvent) for event in events) == 1
        assert not any(isinstance(event, ag.RunFinishedEvent) for event in events)
        assert client.get(f"/api/threads/{thread_id}/runs/failed").is_success


async def test_cancelled_pydantic_stream_persists_partial_turn_and_can_continue() -> None:
    waiting = asyncio.Event()
    closed = asyncio.Event()
    persisted = asyncio.Event()
    requests: list[list[ModelMessage]] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        requests.append(copy.deepcopy(messages))
        if len(requests) > 1:
            yield "Continued."
            return
        try:
            yield "Partial answer."
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    wrapper = PydanticAiAgentWrapper(Agent(FunctionModel(stream_function=model)))
    config = BaseConfig.model_validate({"agents": {"static": {"test": lambda: wrapper}}})
    handler = AgentHandler(config.agents)
    database = Database(DatabaseConfig())
    user = User(id="test")
    await database.setup()
    await handler.setup()
    try:
        await database.create_thread(user_id=user.id, id="thread", name=None, agent_id="test")
        request = schema.AugmentedRunAgentInput.model_validate(
            {
                "threadId": "thread",
                "runId": "cancelled",
                "state": {},
                "tools": [],
                "context": [],
                "forwardedProps": {},
                "messages": [{"id": "question", "role": "user", "content": [{"type": "text", "text": "Hello"}]}],
            }
        )

        async def save(processor: EventProcessor) -> None:
            await database.create_run(processor.extract(include_input_message_ids={"question"}))
            persisted.set()

        response = await handler.run("test", request, user=user, callback=save)

        async def consume() -> None:
            async for _ in response.body_iterator:
                pass

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(waiting.wait(), timeout=5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.wait_for(persisted.wait(), timeout=5)
        await asyncio.wait_for(closed.wait(), timeout=5)
        _, _, history = await database.get_thread_history(user_id=user.id, thread_id="thread", run_id="cancelled")
        saved = pydantic.TypeAdapter(list[schema.AugmentedMessage]).validate_python(history, from_attributes=True)
        assert any(
            isinstance(message, ag.AssistantMessage) and message.content == "Partial answer." for message in saved
        )

        next_question = schema.AugmentedUserMessage.model_validate(
            {"id": "followup", "role": "user", "content": [{"type": "text", "text": "Continue"}]}
        )
        followup = request.model_copy(
            update={"run_id": "continued", "parent_run_id": "cancelled", "messages": [*saved, next_question]}
        )
        events = [event async for event in wrapper.run(followup, user)]
        assert isinstance(events[-1], ag.RunFinishedEvent)
        assert len(requests) == 2
        assert "".join(event.delta for event in events if isinstance(event, ag.TextMessageContentEvent)) == "Continued."
    finally:
        await handler.teardown()
        await database.teardown()
