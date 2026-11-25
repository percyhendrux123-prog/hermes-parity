"""Two-tree harness: same scenario, same scripted model replies, OLD tree vs NEW tree.

For each scenario and mode, each tree gets:

* its own Python (the tree's env; the tree itself on PYTHONPATH, never installed into it);
* a fresh HOME and HERMES_HOME under the scratch dir, with only the recorder plugin enabled;
* a fresh loopback ScriptedModel replaying the scenario's replies (no network, no API cost);
* stubbed tools (driver.py swaps every registry handler for a replay stub).

Mode A (input parity): only the first request is compared: system prompt sections, tool schemas
and order, the message list and request params. The scripted reply is a plain "ok", so no tool
runs.
Mode B (decision parity): the whole turn runs. Compared: every request Hermes builds, every tool
call dispatched (which tool, args, order), every tool result as Hermes saw it, status, and the
final message shape.

Raw bytes live only in this process's memory for the pair comparison. The per-run homes (which
hold Hermes' own session state, so the scenario's text) are deleted unless keep_homes is set.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import checkpoint, differ
from .scripted_model import ScriptedModel

PKG = Path(__file__).resolve().parent
REPO = PKG.parent
PLUGIN_DIR = REPO / "recorder_plugin" / "hermes-parity-recorder"
DRIVER = PKG / "driver.py"
MODE_A_REPLY = [{"content": "ok"}]


@dataclass
class Scenario:
    name: str
    setup: dict
    user_messages: list[str]
    replies: list[dict]
    tool_results: list[dict]

    @classmethod
    def load(cls, path: Path) -> "Scenario":
        setup = json.loads((path / "scenario.json").read_text(encoding="utf-8"))
        users, replies, results = [], [], []
        for line in (path / "replay.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            {"user": lambda i: users.append(i["content"]),
             "reply": lambda i: replies.append({k: i.get(k) for k in ("content", "tool_calls", "finish_reason")}),
             "tool_result": lambda i: results.append({"tool": i["tool"], "result": i["result"]}),
             }[item["type"]](item)
        return cls(setup.get("name", path.name), setup, users, replies, results)

    def driver_view(self, mode: str) -> dict:
        return {**{k: v for k, v in self.setup.items() if k != "name"}, "user_messages": self.user_messages,
                "tool_results": [] if mode == "A" else self.tool_results}


@dataclass
class RunResult:
    records: list[dict]
    raw: dict
    status: str
    seconds: float
    notes: list[str] = field(default_factory=list)
    api_requests: int = 0
    script_exhausted: int = 0


def tree_python(tree: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    for cand in (tree / ".venv" / "bin" / "python", tree / "venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return sys.executable


def tree_sha(tree: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=10).stdout.strip()[:12] or "unknown"
    except OSError:
        return "unknown"


def _prepare_home(base: Path) -> tuple[Path, Path, Path]:
    if base.exists():
        shutil.rmtree(base)
    home = base / "home"
    hermes_home = home / ".hermes"
    (hermes_home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_DIR, hermes_home / "plugins" / PLUGIN_DIR.name,
                    ignore=shutil.ignore_patterns("__pycache__"))
    (hermes_home / "config.yaml").write_text(f"plugins:\n  enabled:\n    - {PLUGIN_DIR.name}\n", encoding="utf-8")
    cwd = base / "work"
    cwd.mkdir()
    return home, hermes_home, cwd


def run_one(tree: Path, python: str, scenario: Scenario, mode: str, scratch: Path, label: str,
            keep_home: bool = False, timeout: int = 600, extra_env: dict | None = None) -> RunResult:
    base = scratch / f"{scenario.name}.{mode}.{label}"
    home, hermes_home, cwd = _prepare_home(base)
    replies = MODE_A_REPLY if mode == "A" else scenario.replies
    scrub = {str(home): "<HOME>", str(cwd): "<CWD>", str(base): "<RUN>",
             str(home.resolve()): "<HOME>", str(cwd.resolve()): "<CWD>", str(base.resolve()): "<RUN>"}
    started = time.time()
    with ScriptedModel(replies, scrub) as sm:
        sink = f"http://127.0.0.1:{sm.port}/parity/sink"
        job = {"scenario": scenario.driver_view(mode), "base_url": sm.base_url, "sink": sink, "mode": mode,
               "out": str(base / "driver.jsonl"), "cwd": str(cwd), "tree": str(tree)}
        (base / "job.json").write_text(json.dumps(job), encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "HERMES_HOME": str(hermes_home),
               "TZ": "UTC", "LANG": "C.UTF-8", "PYTHONHASHSEED": "0", "PYTHONPATH": str(tree),
               "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(base), "NO_COLOR": "1",
               "PARITY_SINK": sink, "PARITY_RECORDER_OUT": str(base / "hooks.jsonl"), **(extra_env or {})}
        # Driver stdout/stderr can echo tool args (Hermes' progress lines): they stay in the run home.
        with (base / "driver.stdout").open("w") as so, (base / "driver.stderr").open("w") as se:
            proc = subprocess.run([python, str(DRIVER), str(base / "job.json")], env=env, cwd=str(cwd),
                                  stdout=so, stderr=se, timeout=timeout, check=False)
        cap = sm.capture
    status = "ok"
    notes: list[str] = []
    if proc.returncode != 0:
        status = f"driver-exit-{proc.returncode}"
        for line in (base / "driver.stderr").read_text(errors="replace").splitlines():
            if line.startswith('{"driver_error"'):
                notes.append(line)
    for line in (base / "driver.jsonl").read_text().splitlines() if (base / "driver.jsonl").exists() else []:
        rec = json.loads(line)
        if rec.get("kind") == "driver_notes":
            notes.extend(rec.get("notes") or [])
    wire = [{"schema": checkpoint.SCHEMA, **r} for r in cap.api_requests]
    hooks = checkpoint.read(base / "hooks.jsonl")
    final = checkpoint.read(base / "driver.jsonl")
    records = wire + hooks + final
    if mode == "A":
        records = [r for r in wire if r["step"] == 0 and r["turn"] == 0]
    result = RunResult(records, dict(cap.raw), status, round(time.time() - started, 2), notes,
                       len(cap.api_requests), cap.exhausted)
    if not keep_home:
        shutil.rmtree(base, ignore_errors=True)
    return result


def load_scenarios(scen_dir: Path) -> list[Scenario]:
    return [Scenario.load(p) for p in sorted(scen_dir.iterdir()) if (p / "scenario.json").exists()]


def run(old: Path, new: Path, scen_dir: Path, out: Path, *, old_python: str | None = None,
        new_python: str | None = None, modes: tuple[str, ...] = ("A", "B"), scratch: Path | None = None,
        keep_homes: bool = False, log=print) -> dict:
    old, new, out = old.resolve(), new.resolve(), out.resolve()
    scratch = (scratch or out / "_homes").resolve()
    out.mkdir(parents=True, exist_ok=True)
    py_old, py_new = tree_python(old, old_python), tree_python(new, new_python)
    meta = {"tool": "hermes-parity", "version": "0.1.0", "old": {"tree": old.name, "sha": tree_sha(old)},
            "new": {"tree": new.name, "sha": tree_sha(new)}, "modes": list(modes), "runs": []}
    all_diffs: list[dict] = []
    for sc in load_scenarios(scen_dir):
        for mode in modes:
            res = {}
            for label, tree, py in (("old", old, py_old), ("new", new, py_new)):
                r = run_one(tree, py, sc, mode, scratch, label, keep_homes)
                checkpoint.write(out / "checkpoints" / f"{sc.name}.{mode}.{label}.jsonl", r.records)
                res[label] = r
            diffs = differ.diff(res["old"].records, res["new"].records, old_raw=res["old"].raw,
                                new_raw=res["new"].raw, context={"scenario": sc.name, "mode": mode})
            for r in res.values():
                r.raw.clear()  # raw bytes end here
            all_diffs.extend(diffs)
            entry = {"scenario": sc.name, "mode": mode, "diffs": len(diffs),
                     **{f"{lbl}_{k}": getattr(r, k) for lbl, r in res.items()
                        for k in ("status", "seconds", "api_requests", "script_exhausted", "notes")},
                     "old_steps": len(res["old"].records), "new_steps": len(res["new"].records)}
            meta["runs"].append(entry)
            log(f"{sc.name} mode {mode}: old={entry['old_status']} new={entry['new_status']} "
                f"steps {entry['old_steps']}/{entry['new_steps']} diffs={len(diffs)}")
    with (out / "diffs.jsonl").open("w", encoding="utf-8") as fh:
        for d in all_diffs:
            fh.write(json.dumps(d, sort_keys=True) + "\n")
    (out / "run.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not keep_homes:
        shutil.rmtree(scratch, ignore_errors=True)
    return meta
