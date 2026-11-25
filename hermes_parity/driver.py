"""Per-tree driver. Runs INSIDE one Hermes tree's own Python env (the tree on PYTHONPATH).

    python driver.py <job.json>

The harness writes job.json: the scenario, the loopback base_url of the scripted model, the sink
URL and the output path. This script:

1. pins what varies by machine/clock (same pins as the tree's own scripts/ci/prompt_surface.py),
   and refuses every non-loopback socket, so no network call can leave the box;
2. replaces EVERY registered tool handler with a stub that returns the scenario's recorded result
   (or a fixed "no recorded result" error), so nothing with side effects can run;
3. builds a real ``AIAgent`` pointed at the scripted model and runs one conversation turn per
   user message;
4. writes a content-free ``final`` checkpoint (message role sequence, final reply hash/len).

It prints nothing that carries content. On failure it prints the exception type and the frames
(file:line), never the message, because a message can quote a prompt or a tool result.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import socket
import sys
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

FIXED_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
SESSION_ID = FIXED_NOW.strftime("%Y%m%d_%H%M%S") + "_hermes_parity"
SCHEMA = "hermes-parity.checkpoint.v1"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _canonical(obj) -> bytes:
    if isinstance(obj, str):
        return obj.encode("utf-8")
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()[:16]


def _pin_environment(notes: list[str]) -> None:
    real_create = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def guarded_create(address, *a, **k):
        if address[0] not in _LOOPBACK:
            raise OSError("hermes-parity: network disabled (loopback only)")
        return real_create(address, *a, **k)

    def guarded_getaddrinfo(host, *a, **k):
        if host not in _LOOPBACK:
            raise OSError("hermes-parity: network disabled (loopback only)")
        return real_getaddrinfo(host, *a, **k)

    socket.create_connection = guarded_create  # type: ignore[assignment]
    socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]

    def pin(desc: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - a tree without this seam is a recorded fact
            notes.append(f"pin-missing:{desc}:{type(exc).__name__}")

    def _time():
        import hermes_time
        hermes_time.now = lambda: FIXED_NOW  # type: ignore[assignment]

    def _check_fns():
        import tools.registry as registry_module
        registry_module._check_fn_cached = lambda _fn: True  # type: ignore[attr-defined]

    def _env_detect():
        from agent import skill_utils
        skill_utils._ENV_DETECT_CACHE.update({"docker": False, "s6": False})

    def _probe():
        from tools import env_probe
        env_probe.get_environment_probe_line = lambda **_k: ""  # type: ignore[assignment]

    pin("hermes_time.now", _time)
    pin("registry._check_fn_cached", _check_fns)
    pin("skill_utils._ENV_DETECT_CACHE", _env_detect)
    pin("env_probe.get_environment_probe_line", _probe)


def _stub_tools(results: list[dict], notes: list[str]) -> None:
    """Swap every handler in the tool registry for a replay stub keyed by (tool name, ordinal)."""
    from tools.registry import registry

    queues: dict[str, list] = {}
    for item in results:
        queues.setdefault(item["tool"], []).append(item["result"])

    def make(name: str):
        def stub(*_args, **_kwargs):
            queue = queues.get(name) or []
            if queue:
                res = queue.pop(0)
                return res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
            return json.dumps({"error": "hermes-parity: no recorded result for this call"})
        return stub

    table = getattr(registry, "_tools", None)
    if not isinstance(table, dict):
        notes.append("stub-missing:registry._tools")
        return
    for name, entry in list(table.items()):
        try:
            table[name] = dataclasses.replace(entry, handler=make(name))
        except Exception:  # noqa: BLE001
            try:
                object.__setattr__(entry, "handler", make(name))
            except Exception as exc:  # noqa: BLE001
                notes.append(f"stub-failed:{name}:{type(exc).__name__}")
    for scoped in (getattr(registry, "_scoped_tools", {}) or {}).values():
        for name, entry in list(scoped.items()):
            scoped[name] = dataclasses.replace(entry, handler=make(name))


def _post(url: str, obj: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()  # noqa: S310 - loopback (harness)


def main(job_path: str) -> int:
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    scenario = job["scenario"]
    notes: list[str] = []
    os.chdir(job["cwd"])
    sys.path.insert(0, job["tree"])
    _pin_environment(notes)

    from run_agent import AIAgent  # noqa: E402 - after pins

    toolsets = scenario.get("enabled_toolsets")
    if toolsets == "platform":  # the stock toolsets for this platform, as prompt_surface.py resolves them
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools
        toolsets = sorted(_get_platform_tools(load_config(), scenario.get("platform", "cli")))

    kwargs = dict(
        model=scenario.get("model", "anthropic/claude-sonnet-4.5"), api_key="hermes-parity-scripted",
        base_url=job["base_url"], provider="custom", quiet_mode=True, save_trajectories=False,
        platform=scenario.get("platform", "cli"), skip_context_files=True, skip_memory=True,
        session_id=SESSION_ID, enabled_toolsets=toolsets,
        max_iterations=int(scenario.get("max_iterations", 8)),
    )
    agent = AIAgent(**kwargs)
    _stub_tools([] if job["mode"] == "A" else scenario.get("tool_results", []), notes)

    final_records = []
    history = None
    for turn, user_message in enumerate(scenario["user_messages"]):
        result = agent.run_conversation(user_message, conversation_history=history)
        history = result.get("messages")
        roles = [m.get("role") for m in (history or [])]
        tool_names = [((tc.get("function") or {}).get("name")) for m in (history or [])
                      for tc in (m.get("tool_calls") or []) if isinstance(tc, dict)]
        final = result.get("final_response") or ""
        shape = {"roles": roles, "tool_call_names": tool_names, "has_final": bool(final),
                 "completed": result.get("completed"), "api_calls": result.get("api_calls")}
        pieces = []
        raw = {}
        for name, obj in (("final.shape", shape), ("final.message", final)):
            data = _canonical(obj)
            rec = {"piece": name, "hash": _digest(data), "len": len(data)}
            pieces.append(rec)
            raw[name] = data.decode("utf-8", "replace")
        final_records.append({"schema": SCHEMA, "turn": turn, "step": 0, "kind": "final", "source": "driver",
                              "pieces": pieces})
        if job.get("sink"):
            _post(job["sink"], {"kind": "final", "turn": turn, "step": 0, "raw": raw})
        if job["mode"] == "A":
            break

    out = Path(job["out"])
    with out.open("a", encoding="utf-8") as fh:
        for rec in final_records:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        fh.write(json.dumps({"schema": SCHEMA, "kind": "driver_notes", "notes": sorted(notes)}) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1]))
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        frames = [f"{Path(f.filename).name}:{f.lineno}" for f in traceback.extract_tb(exc.__traceback__)][-8:]
        print(json.dumps({"driver_error": type(exc).__name__, "frames": frames}), file=sys.stderr)
        sys.exit(3)
