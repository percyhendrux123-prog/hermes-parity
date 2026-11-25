"""Differ: align two runs' checkpoints and emit content-free difference records.

Per difference: scenario, mode, turn, kind, step, piece, first-diff byte offset in the canonical
serialization (None when raw bytes were not available, e.g. comparing stored files offline),
old/new length, length delta, old/new hash, and the count of key paths added/removed.
Missing or extra steps and pieces are differences too. No content, ever.
"""

from __future__ import annotations

from typing import Mapping

from . import canon
from .checkpoint import key

Raw = Mapping[tuple, bytes]


def _pieces(rec: dict | None) -> dict[str, dict]:
    return {p["piece"]: p for p in (rec or {}).get("pieces") or []}


def _meta_pieces(rec: dict) -> dict[str, dict]:
    """Small non-content fields compared as pieces: tool name and status are decisions."""
    out = {}
    for field in ("tool", "status", "error_type"):
        if field in rec:
            data = canon.canonical(rec.get(field))
            out[f"{rec['kind']}.{field}"] = {"piece": f"{rec['kind']}.{field}", "hash": canon.digest(data),
                                             "len": len(data), "_raw": data}
    return out


def diff(old: list[dict], new: list[dict], *, old_raw: Raw | None = None, new_raw: Raw | None = None,
         context: dict | None = None) -> list[dict]:
    context = dict(context or {})
    old_by = {key(r): r for r in old}
    new_by = {key(r): r for r in new}
    out: list[dict] = []
    for k in sorted(set(old_by) | set(new_by)):
        turn, kind, step = k
        base = {**context, "turn": turn, "kind": kind, "step": step}
        o, n = old_by.get(k), new_by.get(k)
        if o is None or n is None:
            out.append({**base, "change": "extra_step" if o is None else "missing_step", "piece": kind,
                        "offset": None, "old_len": None, "new_len": None, "delta": None,
                        "old_hash": None, "new_hash": None})
            continue
        op, np_ = {**_pieces(o), **_meta_pieces(o)}, {**_pieces(n), **_meta_pieces(n)}
        for name in sorted(set(op) | set(np_), key=lambda s: (s.split("[")[0], s)):
            a, b = op.get(name), np_.get(name)
            if a and b and a["hash"] == b["hash"] and a["len"] == b["len"]:
                continue
            rec = {**base, "piece": name}
            if a is None or b is None:
                rec.update(change="extra_piece" if a is None else "missing_piece", offset=None,
                           old_len=a and a["len"], new_len=b and b["len"],
                           delta=None, old_hash=a and a["hash"], new_hash=b and b["hash"])
                out.append(rec)
                continue
            ra = a.get("_raw") if "_raw" in a else (old_raw or {}).get((kind, turn, step, name))
            rb = b.get("_raw") if "_raw" in b else (new_raw or {}).get((kind, turn, step, name))
            offset = canon.first_diff(ra, rb) if ra is not None and rb is not None else None
            sa, sb = set(a.get("shape") or []), set(b.get("shape") or [])
            rec.update(change="changed", offset=offset, old_len=a["len"], new_len=b["len"],
                       delta=b["len"] - a["len"], old_hash=a["hash"], new_hash=b["hash"],
                       paths_added=len(sb - sa), paths_removed=len(sa - sb))
            out.append(rec)
    return out
