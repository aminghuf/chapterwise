"""The real OpenAI-compatible client, against a local stand-in for the DeepSeek API."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from chapterwise.llm import LLMError, OpenAICompatibleLLM


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    state: dict = {"requests": [], "reply": "A summary."}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"path": self.path, "auth": self.headers["Authorization"], "body": body})
            payload = {
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": state["reply"]},
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
            }
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["base_url"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()


def test_sends_chat_request_and_reads_usage(fake_api):
    llm = OpenAICompatibleLLM("sk-test", fake_api["base_url"], "deepseek-flash")
    completion = llm.complete("system prompt", "user prompt")

    assert completion.text == "A summary."
    assert (completion.tokens_in, completion.tokens_out) == (120, 30)
    request = fake_api["requests"][0]
    assert request["path"].endswith("/chat/completions")
    assert request["auth"] == "Bearer sk-test"
    assert request["body"]["model"] == "deepseek-flash"
    assert request["body"]["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user prompt"},
    ]


def test_empty_answer_is_an_error(fake_api):
    fake_api["reply"] = ""
    llm = OpenAICompatibleLLM("sk-test", fake_api["base_url"], "deepseek-flash")
    with pytest.raises(LLMError, match="empty answer"):
        llm.complete("s", "u")
