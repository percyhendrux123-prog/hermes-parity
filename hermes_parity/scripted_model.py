"""Loopback scripted model + checkpoint sink. Stdlib only; runs in the harness process.

* ``POST /v1/chat/completions`` replays the scenario's scripted replies in order (streaming or
  not, whichever Hermes asks for). No model, no network: the model "runs once" when the replies
  were recorded, and both trees receive the same replies.
* ``GET /v1/models`` answers the metadata probe a custom endpoint gets.
* ``POST /parity/sink`` receives raw canonical pieces from the recorder plugin so the differ can
  find the first differing byte. Raw bytes stay in this process's memory only and are dropped
  when the pair is compared; nothing raw is written to disk.

Each incoming request body is split into pieces (canon.iter_request_pieces); the content-free
description goes to the checkpoint list, the raw bytes to ``raw``.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import canon

MODEL_ID = "parity-scripted"


class Capture:
    """Everything one run produced on the harness side. ``raw`` never leaves memory."""

    def __init__(self, replies: list[dict], scrub: dict[str, str]):
        self.replies = list(replies)
        self.scrub = scrub
        self.api_requests: list[dict] = []
        self.sink_records: list[dict] = []
        self.raw: dict[tuple, bytes] = {}
        self.exhausted = 0
        self.lock = threading.Lock()

    def scrub_bytes(self, data: bytes) -> bytes:
        for raw, token in sorted(self.scrub.items(), key=lambda kv: -len(kv[0])):
            if raw:
                data = data.replace(raw.encode(), token.encode())
        return data

    def take_api_request(self, body: dict) -> dict:
        with self.lock:
            turn = max(sum(1 for m in body.get("messages") or [] if m.get("role") == "user") - 1, 0)
            step = sum(1 for r in self.api_requests if r["turn"] == turn)
            pieces = []
            for name, obj in canon.iter_request_pieces(body):
                data = self.scrub_bytes(canon.canonical(obj))
                rec = {"piece": name, "hash": canon.digest(data), "len": len(data)}
                if not isinstance(obj, str):
                    rec["shape"] = canon.shape(obj, canon.keep_depth_for(name))
                pieces.append(rec)
                self.raw[("api_request", turn, step, name)] = data
            self.api_requests.append({"kind": "api_request", "source": "wire", "turn": turn, "step": step, "pieces": pieces})
            if self.replies:
                return self.replies.pop(0)
            self.exhausted += 1
            return {"content": "", "finish_reason": "stop"}

    def take_sink(self, payload: dict) -> None:
        with self.lock:
            key = (payload["kind"], payload.get("turn", 0), payload["step"])
            for name, text in (payload.get("raw") or {}).items():
                self.raw[(*key, name)] = self.scrub_bytes(text.encode("utf-8"))


def _tool_calls(reply: dict) -> list[dict]:
    return [
        {"id": tc["id"], "type": "function",
         "function": {"name": tc["name"], "arguments": json.dumps(tc.get("arguments", {}), ensure_ascii=False)}}
        for tc in reply.get("tool_calls") or []
    ]


def _completion(reply: dict) -> dict:
    calls = _tool_calls(reply)
    msg: dict[str, Any] = {"role": "assistant", "content": reply.get("content")}
    if calls:
        msg["tool_calls"] = calls
    return {
        "id": "chatcmpl-parity", "object": "chat.completion", "created": 1768478400, "model": MODEL_ID,
        "choices": [{"index": 0, "message": msg,
                     "finish_reason": reply.get("finish_reason") or ("tool_calls" if calls else "stop")}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _stream_chunks(reply: dict) -> list[dict]:
    base = {"id": "chatcmpl-parity", "object": "chat.completion.chunk", "created": 1768478400, "model": MODEL_ID}
    chunks = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}]
    if reply.get("content"):
        chunks.append({**base, "choices": [{"index": 0, "delta": {"content": reply["content"]}, "finish_reason": None}]})
    calls = _tool_calls(reply)
    for i, tc in enumerate(calls):
        chunks.append({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": i, **tc}]},
                                            "finish_reason": None}]})
    finish = reply.get("finish_reason") or ("tool_calls" if calls else "stop")
    chunks.append({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    chunks.append({**base, "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    return chunks


class _Handler(BaseHTTPRequestHandler):
    server_version = "hermes-parity"

    def log_message(self, *_a):  # never log request lines: paths are fine, but keep stderr clean
        return

    def _json(self, code: int, obj: Any) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": [
                {"id": MODEL_ID, "object": "model", "owned_by": "hermes-parity", "context_length": 200000}]})
        return self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        cap: Capture = self.server.capture  # type: ignore[attr-defined]
        if self.path == "/parity/sink":
            cap.take_sink(body)
            return self._json(200, {"ok": True})
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": {"message": "unsupported endpoint", "type": "parity"}})
        reply = cap.take_api_request(body)
        if not body.get("stream"):
            return self._json(200, _completion(reply))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for chunk in _stream_chunks(reply):
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class ScriptedModel:
    """Context manager: ``with ScriptedModel(replies, scrub) as sm: sm.base_url``."""

    def __init__(self, replies: list[dict], scrub: dict[str, str] | None = None):
        self.capture = Capture(replies, scrub or {})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.capture = self.capture  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def __enter__(self) -> "ScriptedModel":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        time.sleep(0)
