"""Checkpoint schema and JSONL I/O.

A checkpoint is one step of one run, content-free:

    schema   "hermes-parity.checkpoint.v1"
    turn     user-turn ordinal (0-based)
    step     ordinal of this ``kind`` within the turn (so one extra tool call shifts only tool steps)
    kind     api_request (wire) | api_call | api_response | tool_call | tool_result (hook) | final (driver)
    source   wire | hook | driver
    tool     tool name (tool_call / tool_result only)
    status   tool status (tool_result only)
    pieces   [{"piece": name, "hash": "sha256:<16 hex>", "len": bytes, "shape": [sorted key paths]?}]

Alignment key: (turn, kind, step). Raw text never appears in a checkpoint.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

SCHEMA = "hermes-parity.checkpoint.v1"
STEP_KINDS = ("api_request", "api_call", "api_response", "tool_call", "tool_result", "final")
ALLOWED_FIELDS = {"schema", "turn", "step", "kind", "source", "tool", "status", "error_type", "pieces"}
ALLOWED_PIECE_FIELDS = {"piece", "hash", "len", "shape"}


def key(rec: dict) -> tuple:
    return (int(rec.get("turn", 0)), rec["kind"], int(rec.get("step", 0)))


def validate(rec: dict) -> list[str]:
    """Schema problems for one record ([] = valid)."""
    problems = []
    if rec.get("schema") != SCHEMA:
        problems.append("schema")
    if rec.get("kind") not in STEP_KINDS:
        problems.append("kind")
    extra = set(rec) - ALLOWED_FIELDS
    if extra:
        problems.append("fields:" + ",".join(sorted(extra)))
    for p in rec.get("pieces") or []:
        if set(p) - ALLOWED_PIECE_FIELDS:
            problems.append("piece-fields")
        if not str(p.get("hash", "")).startswith("sha256:"):
            problems.append("piece-hash")
    return problems


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            if rec.get("kind") in STEP_KINDS:
                out.append(rec)
    return out


def write(path: Path, records: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in sorted(records, key=key):
            fh.write(json.dumps({k: v for k, v in rec.items() if k in ALLOWED_FIELDS}, sort_keys=True) + "\n")
