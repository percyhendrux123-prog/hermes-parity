"""Report: fingerprint count table (markdown) plus a JSON file, from a run directory."""

from __future__ import annotations

import json
from pathlib import Path

from . import grouper


def build(run_dir: Path, baseline: Path | None = None) -> dict:
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    diffs = [json.loads(line) for line in (run_dir / "diffs.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip()]
    groups = grouper.group(diffs, grouper.load_baseline(baseline))
    return {"meta": meta, "total_diffs": len(diffs), "fingerprints": len(groups), "groups": groups,
            "baseline": str(baseline) if baseline else None}


def markdown(rep: dict) -> str:
    m = rep["meta"]
    lines = [
        "# hermes-parity report", "",
        f"OLD `{m['old']['sha']}` vs NEW `{m['new']['sha']}`; modes {', '.join(m['modes'])}.",
        f"{rep['total_diffs']} difference records in {rep['fingerprints']} fingerprints.", "",
        "## Runs", "",
        "| scenario | mode | old | new | steps old/new | API requests old/new | diffs |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in m["runs"]:
        lines.append(f"| {r['scenario']} | {r['mode']} | {r['old_status']} | {r['new_status']} | "
                     f"{r['old_steps']}/{r['new_steps']} | {r['old_api_requests']}/{r['new_api_requests']} | "
                     f"{r['diffs']} |")
    notes = sorted({n for r in m["runs"] for k in ("old_notes", "new_notes") for n in r.get(k) or []})
    if notes:
        lines += ["", "Driver notes: " + "; ".join(f"`{n}`" for n in notes)]
    lines += ["", "## Fingerprints", ""]
    if not rep["groups"]:
        lines.append("No differences.")
    else:
        lines += ["| fingerprint | count | kind | piece | offset | length delta | modes | scenarios | new kind | summary |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for g in rep["groups"]:
            new_kind = "-" if rep["baseline"] is None else ("NEW" if g["new_kind"] else "seen")
            lines.append(
                f"| `{g['fingerprint']}` | {g['count']} | {g['kind']} | `{g['piece']}` | {g['offset_bucket']} | "
                f"{g['delta_bucket']} | {','.join(g['modes'])} | {', '.join(g['scenarios'])} | {new_kind} | "
                f"{g['summary']} |")
    return "\n".join(lines) + "\n"


def write(run_dir: Path, baseline: Path | None = None) -> dict:
    rep = build(run_dir, baseline)
    (Path(run_dir) / "report.json").write_text(json.dumps(rep, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md = markdown(rep)
    (Path(run_dir) / "report.md").write_text(md, encoding="utf-8")
    return {"report": rep, "markdown": md}


def write_baseline(run_dir: Path, path: Path) -> int:
    rep = build(run_dir)
    fps = sorted(g["fingerprint"] for g in rep["groups"])
    Path(path).write_text(json.dumps({"fingerprints": fps}, indent=2) + "\n", encoding="utf-8")
    return len(fps)
