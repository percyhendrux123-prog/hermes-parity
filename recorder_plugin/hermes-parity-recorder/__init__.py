"""hermes-parity recorder: observer-only checkpoints from Hermes' documented observer hooks.

What it writes (one JSON object per line, schema ``hermes-parity.checkpoint.v1``):

    {"schema": ..., "turn": 0, "step": 1, "kind": "tool_call", "source": "hook",
     "tool": "read_file", "pieces": [{"piece": "tool_call.args", "hash": "sha256:...", "len": 18,
     "shape": ["path:str"]}]}

Kinds: ``api_call`` (pre_api_request counts), ``api_response`` (post_api_request),
``tool_call`` (pre_tool_call), ``tool_result`` (post_tool_call). ``step`` is the ordinal of that
kind within the turn; ``turn`` is the ordinal of the turn_id in this process.

Raw text is OFF. Two optional channels carry raw text and are the owner's business, never a report:

* ``PARITY_REPLAY_OUT``: the model replies needed to replay the session (a scenario seed file).
* ``PARITY_SINK``: a loopback (127.0.0.1) URL of a running hermes-parity harness, which keeps raw
  piece bytes in memory to locate the first differing byte. Non-loopback URLs are refused.

Every hook returns None (observer-only) and swallows its own errors (fails open).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCHEMA = "hermes-parity.checkpoint.v1"

_lock = threading.Lock()
_turns: dict[str, int] = {}
_steps: dict[tuple[int, str], int] = {}


# ── canonical form (kept identical to hermes_parity.canon; the plugin must stay self-contained) ──

def _canonical(obj: Any) -> bytes:
    if isinstance(obj, str):
        return obj.encode("utf-8")
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]{0,31}$")
# Data pieces keep key names only where they are schema names: args keep top-level parameter names;
# a result dict can be keyed by private values, so every result key is hashed.
_KEEP_DEPTH = {"tool_call.args": 1, "tool_result.body": 0}


def _safe_key(key: Any, depth: int, keep_depth: int | None) -> str:
    text = str(key)
    if _IDENT.match(text) and (keep_depth is None or depth <= keep_depth):
        return text
    return "<k:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8] + ">"


def _shape(obj: Any, keep_depth: int | None = None) -> list[str]:
    out: set[str] = set()

    def walk(value: Any, path: str, depth: int = 1) -> None:
        if isinstance(value, dict):
            if not value:
                out.add(path + "{}")
            for key, child in value.items():
                k = _safe_key(key, depth, keep_depth)
                walk(child, f"{path}.{k}" if path else k, depth + 1)
        elif isinstance(value, list):
            if not value:
                out.add(path + "[]")
            for child in value:
                walk(child, path + "[]", depth)
        else:
            out.add(f"{path}:{type(value).__name__}")

    walk(obj, "")
    return sorted(out)


def _parse_maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _field(obj: Any, name: str) -> Any:
    """Provider objects and plain dicts both appear as ``assistant_message``."""
    if obj is None:
        return None
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _piece(name: str, obj: Any) -> tuple[dict, bytes]:
    data = _canonical(obj)
    rec = {"piece": name, "hash": "sha256:" + hashlib.sha256(data).hexdigest()[:16], "len": len(data)}
    if not isinstance(obj, str):
        rec["shape"] = _shape(obj, _KEEP_DEPTH.get(name))
    return rec, data


# ── output ───────────────────────────────────────────────────────────────────────────────────

def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _out_path() -> Path:
    return Path(os.environ.get("PARITY_RECORDER_OUT") or _home() / "parity" / "checkpoints.jsonl")


def _sink_url() -> str | None:
    url = os.environ.get("PARITY_SINK")
    if not url:
        return None
    host = urllib.parse.urlsplit(url).hostname
    return url if host in ("127.0.0.1", "localhost", "::1") else None


def _write(path: Path, obj: dict, *, keep_order: bool = False) -> None:
    """Checkpoints sort keys; the replay file keeps the original order (key order is replayed bytes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, sort_keys=not keep_order, ensure_ascii=False) + "\n")


def _sink(kind: str, turn: int, step: int, raw: dict[str, bytes]) -> None:
    url = _sink_url()
    if not url:
        return
    body = json.dumps({"kind": kind, "turn": turn, "step": step,
                       "raw": {k: v.decode("utf-8", "replace") for k, v in raw.items()}}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()  # noqa: S310 - loopback only, checked above


def _turn(turn_id: Any) -> int:
    key = str(turn_id or "")
    with _lock:
        if key not in _turns:
            _turns[key] = len(_turns)
        return _turns[key]


def _next_step(turn: int, kind: str) -> int:
    with _lock:
        n = _steps.get((turn, kind), 0)
        _steps[(turn, kind)] = n + 1
        return n


def _emit(kind: str, kwargs: dict, pieces: list[tuple[dict, bytes]], **extra: Any) -> int:
    turn = _turn(kwargs.get("turn_id"))
    step = _next_step(turn, kind)
    rec = {"schema": SCHEMA, "turn": turn, "step": step, "kind": kind, "source": "hook",
           "pieces": [p for p, _ in pieces], **extra}
    _write(_out_path(), rec)
    raw = {p["piece"]: data for p, data in pieces}
    if raw:
        _sink(kind, turn, step, raw)
    return step


def _fail_open(fn):
    def wrapper(**kwargs):
        try:
            fn(**kwargs)
        except Exception:  # noqa: BLE001 - observer must never break the agent loop
            pass
        return None
    wrapper.__name__ = fn.__name__
    return wrapper


# ── hooks ────────────────────────────────────────────────────────────────────────────────────

@_fail_open
def on_pre_api_request(**kwargs):
    counts = {k: kwargs.get(k) for k in ("message_count", "tool_count", "api_call_count")}
    _emit("api_call", kwargs, [_piece("api_call.counts", counts)])


@_fail_open
def on_post_api_request(**kwargs):
    meta = {"finish_reason": kwargs.get("finish_reason"),
            "assistant_tool_call_count": kwargs.get("assistant_tool_call_count"),
            "assistant_content_chars": kwargs.get("assistant_content_chars")}
    _emit("api_response", kwargs, [_piece("api_response.meta", meta)])
    replay = os.environ.get("PARITY_REPLAY_OUT")
    if replay:
        msg = kwargs.get("assistant_message")
        content = _field(msg, "content")
        calls = []
        for tc in _field(msg, "tool_calls") or []:
            fn = _field(tc, "function")
            calls.append({"id": _field(tc, "id"), "name": _field(fn, "name"),
                          "arguments": _parse_maybe_json(_field(fn, "arguments"))})
        _write(Path(replay), {"type": "reply", "turn": _turn(kwargs.get("turn_id")), "content": content,
                              "tool_calls": calls, "finish_reason": kwargs.get("finish_reason")}, keep_order=True)


@_fail_open
def on_pre_tool_call(**kwargs):
    args = _parse_maybe_json(kwargs.get("args"))
    _emit("tool_call", kwargs, [_piece("tool_call.args", args)], tool=kwargs.get("tool_name"))


@_fail_open
def on_post_tool_call(**kwargs):
    result = _parse_maybe_json(kwargs.get("result"))
    _emit("tool_result", kwargs, [_piece("tool_result.body", result)], tool=kwargs.get("tool_name"),
          status=kwargs.get("status"), error_type=kwargs.get("error_type"))
    replay = os.environ.get("PARITY_REPLAY_OUT")
    if replay:
        _write(Path(replay), {"type": "tool_result", "tool": kwargs.get("tool_name"), "result": result},
               keep_order=True)


@_fail_open
def on_pre_llm_call(**kwargs):
    replay = os.environ.get("PARITY_REPLAY_OUT")
    if replay and isinstance(kwargs.get("user_message"), str):
        _write(Path(replay), {"type": "user", "content": kwargs["user_message"]}, keep_order=True)


def register(ctx):
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    # Writes only when the owner set PARITY_REPLAY_OUT; always returns None (injects nothing).
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
