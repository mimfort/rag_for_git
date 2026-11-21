"""Local Messages API fixture for running the real Claude Code client without model access."""

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from uuid import uuid4

PREPARE = "mcp__reviewer__prepare_task_context"
VERIFY = "mcp__reviewer__verify_task_brief"


def _blocks(message):
    content = message["content"]
    return content if isinstance(content, list) else []


def _tool_result(messages, call_id):
    for message in reversed(messages):
        for block in _blocks(message):
            if block.get("tool_use_id") != call_id:
                continue
            if block.get("is_error"):
                raise ValueError(f"Tool {call_id} failed: {block['content']}")
            return block["content"]
    raise ValueError(f"Missing tool result for {call_id}")


def _result_field(messages, call_id, key):
    content = _tool_result(messages, call_id)
    raw = content if isinstance(content, str) else content[0]["text"]
    return json.loads(raw)[key]


def _response(request, references, destination):
    messages = request["messages"]
    calls = [
        block for message in messages if message["role"] == "assistant"
        for block in _blocks(message) if block["type"] == "tool_use"
    ]
    previous = [call["name"] for call in calls]
    if not previous:
        tool = PREPARE
        arguments = {"repo": "example/repo", "key": "PRI-299-smoke",
                     "warm_board": False, "include_brief_evidence": True}
    elif previous == [PREPARE]:
        tool = VERIFY
        arguments = {"evidence_id": _result_field(messages, calls[-1]["id"], "brief_evidence_id"),
                     "references": references}
    elif previous == [PREPARE, VERIFY]:
        tool = "Write"
        arguments = {"file_path": str(destination),
                     "content": _result_field(messages, calls[-1]["id"], "markdown")}
    elif previous == [PREPARE, VERIFY, "Write"]:
        _tool_result(messages, calls[-1]["id"])
        tool, arguments = None, None
    else:
        raise ValueError(f"Unexpected tools: {previous}")
    if tool:
        block = {"type": "tool_use", "id": f"toolu_{uuid4().hex}", "name": tool, "input": {}}
        delta = {"type": "input_json_delta", "partial_json": json.dumps(arguments)}
    else:
        block = {"type": "text", "text": ""}
        delta = {"type": "text_delta", "text": "DONE"}
    message = {
        "id": f"msg_{uuid4().hex}", "type": "message", "role": "assistant", "content": [],
        "model": request["model"], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    return (
        ("message_start", {"message": message}),
        ("content_block_start", {"index": 0, "content_block": block}),
        ("content_block_delta", {"index": 0, "delta": delta}),
        ("content_block_stop", {"index": 0}),
        ("message_delta", {
            "delta": {"stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 1},
        }),
        ("message_stop", {}),
    )


@contextmanager
def local_messages(references, destination):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if self.path.split("?")[0] != "/v1/messages":
                self.send_error(404)
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                events = _response(request, references, destination)
            except (ValueError, KeyError) as error:
                self.send_error(422, str(error))
                return
            body = "".join(
                f"event: {name}\ndata: {json.dumps({'type': name, **payload})}\n\n"
                for name, payload in events
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield {
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "ANTHROPIC_API_KEY": "local-fixture",
                "ANTHROPIC_AUTH_TOKEN": "",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "ENABLE_TOOL_SEARCH": "false",
            }
        finally:
            server.shutdown()
            thread.join()
