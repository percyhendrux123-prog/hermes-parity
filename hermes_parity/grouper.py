"""Grouper: one bug tends to leave the same fingerprint on every record it affects.

fingerprint = sha256(kind | piece path with numeric indices normalized | offset bucket |
                     length-delta sign/bucket | change type)[:12]

Buckets are powers of two, so the same bug at a slightly different byte (a longer user message
upstream) still groups together, while "start of the prompt" and "deep in a tool schema" do not.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from pathlib import Path

_INDEX = re.compile(r"\[\d+\]")


def normalize_piece(piece: str) -> str:
    return _INDEX.sub("[#]", piece)


def bucket(n: int | None) -> str:
    if n is None:
        return "na"
    if n == 0:
        return "0"
    return f"2^{abs(n).bit_length() - 1}"


def delta_bucket(d: int | None) -> str:
    if d is None:
        return "na"
    if d == 0:
        return "0"
    if abs(d) == 1:
        return "+1" if d > 0 else "-1"
    return ("+" if d > 0 else "-") + bucket(d)


def fingerprint(rec: dict) -> str:
    parts = [rec["kind"], normalize_piece(rec["piece"]), bucket(rec.get("offset")),
             delta_bucket(rec.get("delta")), rec.get("change", "changed")]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


_PIECE_WORDS = [
    (re.compile(r"^tools\[(.+)\]\.schema$"), "tool schema for {0}"),
    (re.compile(r"^tools\.order$"), "tool list order"),
    (re.compile(r"^system_prompt\.section\[#\]$"), "a system prompt section"),
    (re.compile(r"^messages\[#\]\.(\w+)$"), "a {0} message sent to the model"),
    (re.compile(r"^request\.params$"), "request parameters (model, stream, limits)"),
    (re.compile(r"^tool_call\.args$"), "arguments of a dispatched tool call"),
    (re.compile(r"^tool_call\.tool$"), "which tool was dispatched"),
    (re.compile(r"^tool_result\.body$"), "a tool result"),
    (re.compile(r"^tool_result\.status$"), "a tool call's status"),
    (re.compile(r"^tool_result\.(tool|error_type)$"), "a tool result's {0} field"),
    (re.compile(r"^final\.shape$"), "the final message shape (roles, tool calls, completion)"),
    (re.compile(r"^final\.message$"), "the final reply text"),
    (re.compile(r"^api_call\.counts$"), "API call counts (messages/tools per request)"),
    (re.compile(r"^api_response\.meta$"), "API response metadata"),
]


def describe_piece(piece: str) -> str:
    norm = normalize_piece(piece)
    for pattern, words in _PIECE_WORDS:
        m = pattern.match(norm)
        if m:
            return words.format(*m.groups())
    return norm


def summary_line(rec: dict, count: int) -> str:
    what = describe_piece(rec["piece"])
    change = rec.get("change")
    if change in ("missing_step", "extra_step"):
        side = "missing in NEW" if change == "missing_step" else "only in NEW"
        return f"{rec['kind']} step {side} ({count}x)"
    if change in ("missing_piece", "extra_piece"):
        side = "missing in NEW" if change == "missing_piece" else "only in NEW"
        return f"{what}: piece {side} ({count}x)"
    d = rec.get("delta") or 0
    size = "same length" if d == 0 else f"{d:+d} bytes"
    where = f"from byte {rec['offset']}" if rec.get("offset") is not None else "offset unknown"
    extra = ""
    if rec.get("paths_added") or rec.get("paths_removed"):
        extra = f", key paths +{rec.get('paths_added', 0)}/-{rec.get('paths_removed', 0)}"
    if d == 0 and not extra:
        extra = ", same structure (reordered or edited in place)"
    return f"{what} changed: {size} {where}{extra} ({count}x)"


def group(diffs: list[dict], baseline: set[str] | None = None) -> list[dict]:
    groups: "OrderedDict[str, dict]" = OrderedDict()
    for rec in diffs:
        fp = fingerprint(rec)
        g = groups.setdefault(fp, {"fingerprint": fp, "kind": rec["kind"], "piece": normalize_piece(rec["piece"]),
                                   "change": rec.get("change"), "offset_bucket": bucket(rec.get("offset")),
                                   "delta_bucket": delta_bucket(rec.get("delta")), "count": 0,
                                   "scenarios": set(), "modes": set(), "pieces": set(), "example": rec})
        g["count"] += 1
        g["scenarios"].add(rec.get("scenario", "?"))
        g["modes"].add(rec.get("mode", "?"))
        g["pieces"].add(rec["piece"])
    out = []
    for g in groups.values():
        ex = g.pop("example")
        g["summary"] = summary_line(ex, g["count"])
        g["example_offset"], g["example_delta"] = ex.get("offset"), ex.get("delta")
        g["new_kind"] = baseline is not None and g["fingerprint"] not in baseline
        for f in ("scenarios", "modes", "pieces"):
            g[f] = sorted(g[f])
        out.append(g)
    out.sort(key=lambda g: (-g["count"], g["kind"], g["piece"]))
    return out


def load_baseline(path: Path | None) -> set[str] | None:
    if not path:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return set(data.get("fingerprints", data if isinstance(data, list) else []))
