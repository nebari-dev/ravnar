import contextlib
import json

import httpx


def header(title: str):
    sep = "#" * 80
    print(sep)
    print(f"# {title}")
    print(sep)


def assert_successful_response(response: httpx.Response) -> httpx.Response:
    if response.is_success:
        return response

    if not response.is_stream_consumed:
        response.read()

    content = "<non-decodable bytes>"
    with contextlib.suppress(Exception):
        content = response.text
        content = json.loads(content)

    raise AssertionError(f"request failed: url={response.url}, status_code={response.status_code}, {content=}")


def main():
    client = httpx.Client(base_url="http://127.0.0.1:8000", timeout=None)
    assert_successful_response(client.get("/health"))

    header("user")
    user = assert_successful_response(client.get("/api/user")).json()
    print(json.dumps(user, indent=2))

    header("info")
    config = assert_successful_response(client.get("/api/config")).json()
    print(json.dumps(config, indent=2))

    file = assert_successful_response(
        client.post(
            "/api/files",
            json={"type": "image", "source": {"type": "url", "value": "https://picsum.photos/200/300"}},
        )
    ).json()
    print(json.dumps(file, indent=2))

    file = assert_successful_response(client.get(f"/api/files/{file['id']}")).json()
    print(json.dumps(file, indent=2))

    content = assert_successful_response(client.get(f"/api/files/{file['id']}/content")).content
    print(b"".join([content[:3], f"<{len(content) - 6} bytes>".encode(), content[-3:]]))

    input_content = assert_successful_response(client.get(f"/api/files/{file['id']}/input-content")).json()
    print(json.dumps(input_content, indent=2))

    # agent_ids = [a["id"] for a in config["agents"]]
    #
    # header("delete existing threads")
    # assert_successful_response(client.request("DELETE", "/api/threads", json={}))
    #
    # header("new thread")
    # thread = assert_successful_response(
    #     client.post(
    #         "/api/threads",
    #         json={
    #             "agentId": agent_ids[0],
    #         },
    #     )
    # ).json()
    # print(json.dumps(thread, indent=2))
    #
    # header("new run in same thread with frontend tool call")
    #
    # class CheerInput(pydantic.BaseModel):
    #     name: str = pydantic.Field(description="Name of the user")
    #
    # tool_name: str = "cheer_up_user"
    # tool_call_id: str | None = None
    # tool_call_arguments_json_deltas: list[str] = []
    # with httpx_sse.connect_sse(
    #     client,
    #     "POST",
    #     f"/api/threads/{thread['id']}/run",
    #     json={
    #         "messages": [
    #             {
    #                 "id": str(uuid.uuid4()),
    #                 "role": "user",
    #                 "content": "I'm Huginn and I'm feeling a little down today.",
    #             },
    #         ],
    #         "tools": [
    #             {
    #                 "name": tool_name,
    #                 "description": "Cheer up the user",
    #                 "parameters": CheerInput.model_json_schema(),
    #             },
    #         ],
    #     },
    # ) as event_source:
    #     assert_successful_response(event_source.response)
    #     for sse in event_source.iter_sse():
    #         event = sse.json()
    #         print(json.dumps(event, indent=2))
    #         match event["type"]:
    #             case "TOOL_CALL_START":
    #                 assert event["toolCallName"] == tool_name
    #                 tool_call_id = event["toolCallId"]
    #             case "TOOL_CALL_ARGS":
    #                 tool_call_arguments_json_deltas.append(event["delta"])
    #             case "TOOL_CALL_END":
    #                 assert event["toolCallId"] == tool_call_id
    #
    # tool_call = ag_ui.core.ToolCall(
    #     id=tool_call_id,
    #     function=ag_ui.core.FunctionCall(name=tool_name, arguments="".join(tool_call_arguments_json_deltas)),
    # )
    # print(tool_call.model_dump_json(indent=2))
    #
    # thread = assert_successful_response(client.get(f"/api/threads/{thread['id']}")).json()
    # print(json.dumps(thread, indent=2))
    #
    # header("new run in same thread with frontend tool call result")
    # with httpx_sse.connect_sse(
    #     client,
    #     "POST",
    #     f"/api/threads/{thread['id']}/run",
    #     json={
    #         "messages": [
    #             {
    #                 "id": str(uuid.uuid4()),
    #                 "content": json.dumps({"successful": True}),
    #                 "role": "tool",
    #                 "toolCallId": tool_call_id,
    #             },
    #         ],
    #     },
    # ) as event_source:
    #     assert_successful_response(event_source.response)
    #     for sse in event_source.iter_sse():
    #         event = sse.json()
    #         print(json.dumps(event, indent=2))
    #
    # thread = assert_successful_response(client.get(f"/api/threads/{thread['id']}")).json()
    # print(json.dumps(thread, indent=2))
    #
    # header("list threads")
    # thread = assert_successful_response(client.get("/api/threads", params={"sortBy": "createdAt"})).json()
    # print(json.dumps(thread, indent=2))


if __name__ == "__main__":
    main()
