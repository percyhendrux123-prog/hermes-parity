"""hermes-parity CLI.

    hermes-parity record install --home <HERMES_HOME>     copy + enable the recorder plugin there
    hermes-parity record scenario <replay.jsonl> <out_dir> --toolsets file [--platform cli]
    hermes-parity run --old <tree> --new <tree> --scenarios <dir> --out <run_dir>
                      [--old-python P] [--new-python P] [--modes A,B] [--baseline F] [--keep-homes]
    hermes-parity report <run_dir> [--baseline F] [--write-baseline F]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from . import harness, report


def _record_install(args) -> int:
    home = Path(args.home).expanduser().resolve()
    live = (Path.home() / ".hermes").resolve()
    if home == live and not args.i_mean_it:
        print("refusing to touch the live ~/.hermes; pass --i-mean-it to install there", file=sys.stderr)
        return 2
    dest = home / "plugins" / harness.PLUGIN_DIR.name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(harness.PLUGIN_DIR, dest, ignore=shutil.ignore_patterns("__pycache__"))
    print(f"installed {dest}")
    print(f"enable it: add '{harness.PLUGIN_DIR.name}' to plugins.enabled in {home / 'config.yaml'}")
    print("set PARITY_REPLAY_OUT=<owner file> to also record replies for replay (raw text, keep it private)")
    return 0


def _record_scenario(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    setup = {"name": out.name, "platform": args.platform, "model": args.model,
             "enabled_toolsets": [t for t in args.toolsets.split(",") if t]}
    (out / "scenario.json").write_text(json.dumps(setup, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(args.replay, out / "replay.jsonl")
    print(f"scenario written to {out} (replay.jsonl is raw text: owner-only, never shared)")
    return 0


def _run(args) -> int:
    scratch = Path(args.scratch) if args.scratch else Path(tempfile.mkdtemp(prefix="hermes-parity-"))
    meta = harness.run(Path(args.old), Path(args.new), Path(args.scenarios), Path(args.out),
                       old_python=args.old_python, new_python=args.new_python,
                       modes=tuple(m.strip().upper() for m in args.modes.split(",") if m.strip()),
                       scratch=scratch, keep_homes=args.keep_homes,
                       log=lambda s: print(s, file=sys.stderr))
    out = report.write(Path(args.out), Path(args.baseline) if args.baseline else None)
    print(out["markdown"])
    bad = [r for r in meta["runs"] if r["old_status"] != "ok" or r["new_status"] != "ok"]
    return 1 if bad else 0


def _report(args) -> int:
    if args.write_baseline:
        n = report.write_baseline(Path(args.run_dir), Path(args.write_baseline))
        print(f"wrote {n} fingerprints to {args.write_baseline}", file=sys.stderr)
    out = report.write(Path(args.run_dir), Path(args.baseline) if args.baseline else None)
    print(out["markdown"])
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hermes-parity", description="Old-vs-new Hermes parity checker")
    sub = p.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="recorder plugin and scenario helpers")
    rsub = rec.add_subparsers(dest="rcmd", required=True)
    ri = rsub.add_parser("install", help="copy the recorder plugin into a HERMES_HOME")
    ri.add_argument("--home", required=True)
    ri.add_argument("--i-mean-it", action="store_true")
    ri.set_defaults(fn=_record_install)
    rs = rsub.add_parser("scenario", help="turn a recorder replay file into a scenario dir")
    rs.add_argument("replay")
    rs.add_argument("out")
    rs.add_argument("--toolsets", default="")
    rs.add_argument("--platform", default="cli")
    rs.add_argument("--model", default="anthropic/claude-sonnet-4.5")
    rs.set_defaults(fn=_record_scenario)

    r = sub.add_parser("run", help="run scenarios through OLD and NEW trees")
    r.add_argument("--old", required=True)
    r.add_argument("--new", required=True)
    r.add_argument("--scenarios", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--old-python")
    r.add_argument("--new-python")
    r.add_argument("--modes", default="A,B")
    r.add_argument("--baseline")
    r.add_argument("--scratch", help="where per-run HOME/HERMES_HOME dirs go (default: a temp dir)")
    r.add_argument("--keep-homes", action="store_true", help="keep per-run homes (they hold scenario text)")
    r.set_defaults(fn=_run)

    rp = sub.add_parser("report", help="print the fingerprint table for a run dir")
    rp.add_argument("run_dir")
    rp.add_argument("--baseline")
    rp.add_argument("--write-baseline")
    rp.set_defaults(fn=_report)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
