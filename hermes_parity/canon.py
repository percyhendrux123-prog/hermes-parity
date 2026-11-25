"""Canonical serialization, hashing and shape. Stdlib only.

The canonical form keeps key ORDER on purpose: the model reads the bytes Hermes sends, and a
key-order change moves bytes (and breaks the prompt-cache prefix) even when the parsed object is
equal. ``shape`` is the order-free view (sorted key paths) for telling "same structure, different
bytes" from "structure changed".
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

# Same splitting rule on both sides: a new section starts at every markdown heading line.
# Concatenating the sections gives back the original text byte for byte.
_SECTION_START = re.compile(r"(?m)^(?=#{1,6} )")


def canonical(obj: Any) -> bytes:
    """Order-preserving, whitespace-free JSON bytes; strings pass through as UTF-8."""
    if isinstance(obj, str):
        return obj.encode("utf-8")
    if isinstance(obj, bytes):
        return obj
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()[:16]


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]{0,31}$")


def safe_key(key: Any, depth: int, keep_depth: int | None) -> str:
    """Key names are structure, but a data dict can be keyed by private values (emails, names).
    Keys deeper than ``keep_depth``, or not identifier-shaped, are replaced by a short hash."""
    text = str(key)
    if _IDENT.match(text) and (keep_depth is None or depth <= keep_depth):
        return text
    return "<k:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8] + ">"


def shape(obj: Any, keep_depth: int | None = None) -> list[str]:
    """Sorted key paths with list indices collapsed to ``[]``. Values never appear."""
    out: set[str] = set()

    def walk(value: Any, path: str, depth: int = 1) -> None:
        if isinstance(value, dict):
            if not value:
                out.add(path + "{}")
            for key, child in value.items():
                k = safe_key(key, depth, keep_depth)
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


# Pieces that carry user/tool data keep key names only near the top; schema-like pieces keep all.
DATA_KEEP_DEPTH = {"messages": 4, "tool_call.args": 1, "tool_result.body": 0}


def keep_depth_for(piece_name: str) -> int | None:
    for prefix, depth in DATA_KEEP_DEPTH.items():
        if piece_name.startswith(prefix):
            return depth
    return None


def piece(name: str, obj: Any) -> tuple[dict, bytes]:
    """Content-free description of one comparable piece, plus its raw bytes (memory only)."""
    data = canonical(obj)
    rec = {"piece": name, "hash": digest(data), "len": len(data)}
    if not isinstance(obj, (str, bytes)):
        rec["shape"] = shape(obj, keep_depth_for(name))
    return rec, data


def split_sections(text: str) -> list[str]:
    parts = [p for p in _SECTION_START.split(text) if p != ""]
    return parts or [""]


def first_diff(a: bytes, b: bytes) -> int | None:
    """Byte offset of the first difference, or None if equal."""
    if a == b:
        return None
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def message_text(content: Any) -> Any:
    """OpenAI content may be a string or a list of parts; both canonicalize as-is."""
    return content if content is not None else ""


def iter_request_pieces(body: dict) -> Iterable[tuple[str, Any]]:
    """Split one chat.completions request body into named pieces.

    system_prompt.section[i]  each markdown section of the system message(s)
    tools.order               the ordered list of tool names (order is a real wire difference)
    tools[<name>].schema      each tool's function object
    messages[i].<role>        each non-system message (role carried in the piece name)
    request.params            every other top-level key (model, temperature, max_tokens, ...)
    """
    messages = body.get("messages") or []
    system_texts = []
    rest = []
    for msg in messages:
        if msg.get("role") in ("system", "developer") and not rest:
            content = msg.get("content")
            system_texts.append(content if isinstance(content, str) else json.dumps(content))
        else:
            rest.append(msg)
    for i, section in enumerate(split_sections("\n\n".join(system_texts))):
        yield f"system_prompt.section[{i}]", section
    tools = body.get("tools") or []
    names = [((t.get("function") or {}).get("name") or t.get("name") or f"#{i}") for i, t in enumerate(tools)]
    yield "tools.order", names
    for name, tool in zip(names, tools):
        yield f"tools[{name}].schema", tool.get("function", tool)
    for i, msg in enumerate(rest):
        yield f"messages[{i}].{msg.get('role', '?')}", msg
    params = {k: v for k, v in body.items() if k not in ("messages", "tools")}
    yield "request.params", params
