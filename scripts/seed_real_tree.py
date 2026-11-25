#!/usr/bin/env python3
"""Make a seeded copy of a REAL Hermes tree for an end-to-end detection check (never edits the source).

    python3 scripts/seed_real_tree.py <hermes-tree> <dest>

Copies the tree (without .git, website, apps, ui-tui, tests, node_modules) and injects the same
three known differences as the unit-test fixture, at real code sites:

1. key-order change in a tool schema   tools/file_tools.py   read_file: swap ``offset`` and ``limit``
2. stripped newline in a prompt section agent/prompt_builder.py TASK_COMPLETION_GUIDANCE line end
3. missing tool description            tools/file_tools.py   search_files: drop ``description``

Then: ``hermes-parity run --old <hermes-tree> --new <dest> ...`` should give exactly three
fingerprints. Anchors are checked; a tree where an anchor moved fails loudly instead of seeding less.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

EXCLUDE = (".git", "website", "apps", "ui-tui", "tests", "node_modules", "__pycache__", "*.egg-info")


def _swap_read_file_params(text: str) -> str:
    m = re.search(r'READ_FILE_SCHEMA = \{.*?\n\}\n', text, re.S)
    assert m, "READ_FILE_SCHEMA not found"
    block = m.group(0)
    lines = block.splitlines(keepends=True)
    i = next(k for k, ln in enumerate(lines) if ln.lstrip().startswith('"offset": {'))
    j = next(k for k, ln in enumerate(lines) if ln.lstrip().startswith('"limit": {'))
    assert j == i + 1, "offset/limit not adjacent"
    a, b = lines[i].rstrip("\n"), lines[j].rstrip("\n")
    # keep the trailing-comma layout: the last property has no comma
    a_core, b_core = a.rstrip(","), b.rstrip(",")
    lines[i] = b_core + ("," if a.endswith(",") else "") + "\n"
    lines[j] = a_core + ("," if b.endswith(",") else "") + "\n"
    return text.replace(block, "".join(lines))


def _strip_guidance_newline(text: str) -> str:
    anchor = '"execution returned.\\n"'
    assert text.count(anchor) == 1, "TASK_COMPLETION_GUIDANCE anchor not unique"
    return text.replace(anchor, '"execution returned."')


def _drop_search_description(text: str) -> str:
    m = re.search(r'(SEARCH_FILES_SCHEMA = \{\n    "name": "search_files",\n)(    "description": .*?\n)(    "parameters")',
                  text, re.S)
    assert m, "search_files description anchor not found"
    return text.replace(m.group(0), m.group(1) + m.group(3))


SEEDS = [
    ("tools/file_tools.py", _swap_read_file_params),
    ("agent/prompt_builder.py", _strip_guidance_newline),
    ("tools/file_tools.py", _drop_search_description),
]


def main(src: str, dest: str) -> int:
    src_p, dest_p = Path(src).resolve(), Path(dest).resolve()
    if dest_p.exists():
        shutil.rmtree(dest_p)
    shutil.copytree(src_p, dest_p, ignore=shutil.ignore_patterns(*EXCLUDE), symlinks=True)
    for rel, fn in SEEDS:
        path = dest_p / rel
        before = path.read_text(encoding="utf-8")
        after = fn(before)
        assert after != before, f"seed did nothing in {rel}"
        path.write_text(after, encoding="utf-8")
    print(f"seeded copy at {dest_p} (3 seeds)")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
