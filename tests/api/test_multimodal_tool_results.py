import base64
import uuid
from collections.abc import AsyncIterator

import ag_ui.core
import httpx_sse
import pydantic
import pytest

from _ravnar import schema
from _ravnar.agents import Agent
from _ravnar.config import BaseConfig
from _ravnar.file_storage import RAVNAR_PROVIDER
from tests.utils import TestClient

IMAGE_BYTES = b"fake-png-bytes"


class MultimodalToolResultAgent(Agent):
    """Emits tool results with inline data parts and a foreign provider file handle."""

    async def run(self, input: ag_ui.core.RunAgentInput, user) -> AsyncIterator[ag_ui.core.Event]:
        yield ag_ui.core.RunStartedEvent(
            thread_id=input.thread_id, run_id=input.run_id, parent_run_id=input.parent_run_id
        )
        yield ag_ui.core.TextMessageStartEvent(message_id="a1")
        yield ag_ui.core.TextMessageEndEvent(message_id="a1")

        # tool result with a text part and an inline image part
        yield ag_ui.core.ToolCallStartEvent(
            tool_call_id="tc-inline", tool_call_name="get_chart", parent_message_id="a1"
        )
        yield ag_ui.core.ToolCallEndEvent(tool_call_id="tc-inline")
        yield ag_ui.core.ToolCallResultEvent(
            message_id="tr-inline",
            tool_call_id="tc-inline",
            content=[
                ag_ui.core.TextPart(text="chart attached"),
                ag_ui.core.ImagePart(
                    source=ag_ui.core.DataSource(value=base64.b64encode(IMAGE_BYTES).decode(), mime_type="image/png")
                ),
            ],
        )

        # tool result referencing another provider's storage; the part must be dropped
        yield ag_ui.core.ToolCallStartEvent(
            tool_call_id="tc-foreign", tool_call_name="get_provider_file", parent_message_id="a1"
        )
        yield ag_ui.core.ToolCallEndEvent(tool_call_id="tc-foreign")
        yield ag_ui.core.ToolCallResultEvent(
            message_id="tr-foreign",
            tool_call_id="tc-foreign",
            content=[
                ag_ui.core.TextPart(text="see below"),
                ag_ui.core.ImagePart(source=ag_ui.core.FileSource(value="fileid_abc123", provider="anthropic")),
            ],
        )

        yield ag_ui.core.RunFinishedEvent(thread_id=input.thread_id, run_id=input.run_id)


@pytest.fixture
def mm_client():
    config = BaseConfig.model_validate(
        {
            "security": {"authenticator": "tests.utils.HeaderAuthenticator"},
            "agents": {"static": {"multimodal": MultimodalToolResultAgent}},
        }
    )
    with TestClient.from_config(config) as client:
        yield client


class TestMultimodalToolResults:
    def run_to_completion(self, client, *, thread_id):
        with httpx_sse.connect_sse(
            client,
            "POST",
            f"/api/threads/{thread_id}/runs",
            json={"messages": [{"role": "user", "content": [{"type": "text", "text": "give me the chart"}]}]},
        ) as event_source:
            event_source.response.raise_for_status()
            list(event_source.iter_sse())

    def get_messages(self, client, thread_id) -> list[schema.AugmentedMessage]:
        response = client.get(f"/api/threads/{thread_id}/messages").raise_for_status()
        return pydantic.TypeAdapter(list[schema.AugmentedMessage]).validate_json(response.content)

    def test_inline_tool_result_file_is_stored_and_replayed(self, mm_client):
        thread = mm_client.post("/api/threads", json={"agentId": "multimodal", "name": "mm"}).raise_for_status()
        thread_id = thread.json()["id"]

        self.run_to_completion(mm_client, thread_id=thread_id)

        messages = self.get_messages(mm_client, thread_id)
        tool_messages = [m for m in messages if m.role == "tool"]
        by_id = {m.id: m for m in tool_messages}

        inline = by_id["tr-inline"]
        assert isinstance(inline.content, list)
        assert inline.content[0].text == "chart attached"

        image_part = inline.content[1]
        assert image_part.type == "image"
        assert image_part.source.type == "file"
        assert image_part.source.provider == RAVNAR_PROVIDER

        file_id = uuid.UUID(image_part.source.value)
        content_response = mm_client.get(f"/api/files/{file_id}/content").raise_for_status()
        assert content_response.content == IMAGE_BYTES
        assert content_response.headers["Content-Type"] == "image/png"

    def test_foreign_file_source_part_is_dropped(self, mm_client):
        thread = mm_client.post("/api/threads", json={"agentId": "multimodal", "name": "mm"}).raise_for_status()
        thread_id = thread.json()["id"]

        self.run_to_completion(mm_client, thread_id=thread_id)

        messages = self.get_messages(mm_client, thread_id)
        foreign = next(m for m in messages if m.id == "tr-foreign")
        assert foreign.content == [ag_ui.core.TextPart(text="see below")]
