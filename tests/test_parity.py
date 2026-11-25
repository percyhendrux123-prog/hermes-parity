"""hermes-parity tests. Run: python3 -m unittest discover -s tests -v

The two-tree tests run a small fake Hermes tree (tests/fixtures/fake_tree) through the real
harness, scripted model, recorder plugin and driver, with this interpreter as each tree's Python.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hermes_parity import canon, checkpoint, cli, differ, grouper, harness, report  # noqa: E402

FIX = REPO / "tests" / "fixtures"
FAKE = FIX / "fake_tree"
CANARY = "PARITYCANARY7Q3ZX9"

# The three seeded differences, applied to a patched copy of the fake tree.
SEEDS = [
    ("tools/file_tools.py",  # 1. key-order change in a tool schema
     '            "offset": {"type": "integer", "description": "First line", "default": 1},\n'
     '            "limit": {"type": "integer", "description": "Max lines", "default": 2000},\n',
     '            "limit": {"type": "integer", "description": "Max lines", "default": 2000},\n'
     '            "offset": {"type": "integer", "description": "First line", "default": 1},\n'),
    ("prompt.py",  # 2. stripped trailing newline in a prompt section
     '    "Report what real execution returned.\\n"\n    "\\n"\n',
     '    "Report what real execution returned.\\n"\n'),
    ("tools/file_tools.py",  # 3. missing tool description
     '    "description": "Search file contents with a regex.",\n', ''),
]


def make_patched_copy(dest: Path) -> Path:
    shutil.copytree(FAKE, dest, ignore=shutil.ignore_patterns("__pycache__"))
    for rel, old, new in SEEDS:
        path = dest / rel
        text = path.read_text()
        assert text.count(old) == 1, f"seed anchor not unique in {rel}"
        path.write_text(text.replace(old, new))
    return dest


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent.parent if path.parent.name == "tools" else path.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


def run_pair(old: Path, new: Path, scenarios: Path, out: Path, modes=("A", "B"), keep_homes=False) -> dict:
    meta = harness.run(old, new, scenarios, out, old_python=sys.executable, new_python=sys.executable,
                       modes=modes, scratch=out.parent / (out.name + "-homes"), keep_homes=keep_homes,
                       log=lambda _s: None)
    return {"meta": meta, **report.write(out)}


class CanonTests(unittest.TestCase):
    def test_canonical_keeps_key_order(self):
        self.assertNotEqual(canon.canonical({"a": 1, "b": 2}), canon.canonical({"b": 2, "a": 1}))
        self.assertEqual(canon.shape({"a": 1, "b": 2}), canon.shape({"b": 2, "a": 1}))

    def test_sections_round_trip(self):
        text = "pre\n\n# One\nx\n\n## Two\ny\n# Three\n"
        parts = canon.split_sections(text)
        self.assertEqual("".join(parts), text)
        self.assertEqual(len(parts), 4)

    def test_first_diff(self):
        self.assertIsNone(canon.first_diff(b"abc", b"abc"))
        self.assertEqual(canon.first_diff(b"abc", b"abX"), 2)
        self.assertEqual(canon.first_diff(b"abc", b"ab"), 2)

    def test_shape_hashes_private_keys(self):
        s = canon.shape({"user@example.com": 1, "ok": {"deep": 2}}, keep_depth=1)
        self.assertFalse(any("example" in p or "deep" in p for p in s))
        self.assertTrue(any(p.startswith("ok.") for p in s))
        self.assertFalse(any(CANARY in p for p in canon.shape({CANARY: 1}, keep_depth=0)))

    def test_recorder_canon_matches_package(self):
        plugin = load_module(harness.PLUGIN_DIR / "__init__.py", "parity_recorder_under_test")
        obj = {"b": [1, {"c": "x"}], "a": None, "ü": "é"}
        self.assertEqual(plugin._canonical(obj), canon.canonical(obj))
        self.assertEqual(plugin._shape(obj), canon.shape(obj))


class DifferGrouperTests(unittest.TestCase):
    def rec(self, kind, step, pieces, **extra):
        return {"schema": checkpoint.SCHEMA, "turn": 0, "step": step, "kind": kind, "source": "wire",
                "pieces": pieces, **extra}

    def test_missing_and_extra_steps(self):
        p = {"piece": "tool_call.args", "hash": "sha256:1", "len": 3}
        old = [self.rec("tool_call", 0, [p]), self.rec("tool_call", 1, [p])]
        new = [self.rec("tool_call", 0, [p]), self.rec("tool_result", 0, [p])]
        changes = sorted(d["change"] for d in differ.diff(old, new))
        self.assertEqual(changes, ["extra_step", "missing_step"])

    def test_offsets_from_raw(self):
        a = {"piece": "system_prompt.section[2]", "hash": canon.digest(b"hello\n"), "len": 6}
        b = {"piece": "system_prompt.section[2]", "hash": canon.digest(b"hello"), "len": 5}
        raw_a = {("api_request", 0, 0, a["piece"]): b"hello\n"}
        raw_b = {("api_request", 0, 0, b["piece"]): b"hello"}
        [d] = differ.diff([self.rec("api_request", 0, [a])], [self.rec("api_request", 0, [b])],
                          old_raw=raw_a, new_raw=raw_b)
        self.assertEqual((d["offset"], d["delta"]), (5, -1))
        [g] = grouper.group([d])
        self.assertIn("-1 bytes from byte 5", g["summary"])

    def test_baseline_flags_new_kind(self):
        d = {"kind": "api_request", "piece": "tools[x].schema", "offset": 120, "delta": 34, "change": "changed"}
        fp = grouper.fingerprint(d)
        self.assertFalse(grouper.group([d], {fp})[0]["new_kind"])
        self.assertTrue(grouper.group([d], {"other"})[0]["new_kind"])
        self.assertEqual(grouper.fingerprint({**d, "piece": "tools[x].schema", "offset": 127}), fp)

    def test_index_normalization(self):
        self.assertEqual(grouper.normalize_piece("messages[12].tool"), "messages[#].tool")


class TwoTreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="hermes-parity-test-"))
        cls.patched = make_patched_copy(cls.tmp / "fake_tree_patched")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_clean_pair_zero_diffs(self):
        out = run_pair(FAKE, FAKE, FIX / "scenarios" / "seeded", self.tmp / "clean")
        self.assertEqual(out["report"]["total_diffs"], 0)
        runs = out["meta"]["runs"]
        self.assertTrue(all(r["old_status"] == r["new_status"] == "ok" for r in runs), runs)
        mode_b = [r for r in runs if r["mode"] == "B"][0]
        self.assertEqual(mode_b["old_api_requests"], 3)
        # Mode B really dispatched both tools through the stubs (hook checkpoints exist).
        recs = checkpoint.read(self.tmp / "clean" / "checkpoints" / "s1_search_then_read.B.old.jsonl")
        self.assertEqual([r["tool"] for r in recs if r["kind"] == "tool_call"], ["search_files", "read_file"])
        self.assertTrue(all(not checkpoint.validate(r) for r in recs), [checkpoint.validate(r) for r in recs])

    def test_seeded_three_fingerprints(self):
        out = run_pair(FAKE, self.patched, FIX / "scenarios" / "seeded", self.tmp / "seeded")
        groups = out["report"]["groups"]
        self.assertEqual(len(groups), 3, [g["summary"] for g in groups])
        by_piece = {g["piece"]: g for g in groups}
        self.assertEqual(set(by_piece), {"tools[read_file].schema", "system_prompt.section[#]",
                                         "tools[search_files].schema"})

        orig = load_module(FAKE / "tools" / "file_tools.py", "fake_orig_tools")
        pat = load_module(self.patched / "tools" / "file_tools.py", "fake_pat_tools")
        ordr = by_piece["tools[read_file].schema"]
        exp = canon.first_diff(canon.canonical(orig.READ_FILE_SCHEMA), canon.canonical(pat.READ_FILE_SCHEMA))
        self.assertEqual((ordr["example_offset"], ordr["example_delta"]), (exp, 0))
        desc = by_piece["tools[search_files].schema"]
        exp = canon.first_diff(canon.canonical(orig.SEARCH_FILES_SCHEMA), canon.canonical(pat.SEARCH_FILES_SCHEMA))
        exp_delta = len(canon.canonical(pat.SEARCH_FILES_SCHEMA)) - len(canon.canonical(orig.SEARCH_FILES_SCHEMA))
        self.assertEqual((desc["example_offset"], desc["example_delta"]), (exp, exp_delta))

        prompt_orig = load_module(FAKE / "prompt.py", "fake_orig_prompt")
        sec = by_piece["system_prompt.section[#]"]
        self.assertEqual(sec["pieces"], ["system_prompt.section[1]"])
        self.assertEqual((sec["example_offset"], sec["example_delta"]),
                         (len(prompt_orig.TASK_GUIDANCE) - 1, -1))
        # Each seeded diff shows in Mode A (first request) and Mode B (all three requests).
        self.assertEqual(sorted(g["count"] for g in groups), [4, 4, 4])
        self.assertEqual({tuple(g["modes"]) for g in groups}, {("A", "B")})

    def test_privacy_canary(self):
        out_dir = self.tmp / "canary"
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            rc = cli.main(["run", "--old", str(FAKE), "--new", str(self.patched),
                           "--scenarios", str(FIX / "scenarios" / "canary"), "--out", str(out_dir),
                           "--old-python", sys.executable, "--new-python", sys.executable,
                           "--scratch", str(self.tmp / "canary-homes")])
        self.assertEqual(rc, 0)
        self.assertGreater(len(buf_out.getvalue()), 0)
        for name, text in (("stdout", buf_out.getvalue()), ("stderr", buf_err.getvalue())):
            self.assertNotIn(CANARY, text, name)
        files = [p for p in out_dir.rglob("*") if p.is_file()]
        self.assertTrue(any(p.name == "report.md" for p in files))
        self.assertGreater(len(files), 4)
        for p in files:
            self.assertNotIn(CANARY, p.read_text(errors="replace"), str(p))
        self.assertFalse((self.tmp / "canary-homes").exists(), "per-run homes must be deleted by default")

        # Kept homes: the recorder's own checkpoint files must also be canary-free. (job.json there is a
        # copy of the owner's replay file and is exempt, like the replay file itself.)
        kept = self.tmp / "canary-kept"
        run_pair(FAKE, self.patched, FIX / "scenarios" / "canary", kept, keep_homes=True)
        recs = list((self.tmp / "canary-kept-homes").rglob("hooks.jsonl")) + \
            list((self.tmp / "canary-kept-homes").rglob("driver.jsonl"))
        self.assertGreaterEqual(len(recs), 4)
        kinds = set()
        for p in recs:
            text = p.read_text()
            self.assertNotIn(CANARY, text, str(p))
            kinds |= {json.loads(line)["kind"] for line in text.splitlines()}
        self.assertTrue({"tool_call", "tool_result", "final"} <= kinds, kinds)


class DecisionParityTests(unittest.TestCase):
    """Mode B must see a changed decision even when every request input is identical."""

    def test_refusing_a_tool_shows_as_missing_dispatch(self):
        with tempfile.TemporaryDirectory(prefix="hermes-parity-decision-") as d:
            tmp = Path(d)
            new = tmp / "fake_tree_refuses"
            shutil.copytree(FAKE, new, ignore=shutil.ignore_patterns("__pycache__"))
            path = new / "run_agent.py"
            text = path.read_text()
            anchor = "if name not in self.enabled:"
            self.assertEqual(text.count(anchor), 1)
            path.write_text(text.replace(anchor, 'if name not in self.enabled or name == "search_files":'))
            out = run_pair(FAKE, new, FIX / "scenarios" / "seeded", tmp / "run")
            groups = out["report"]["groups"]
            self.assertTrue(groups)
            self.assertEqual({m for g in groups for m in g["modes"]}, {"B"}, "Mode A input must be identical")
            missing = {(g["kind"], g["change"]) for g in groups}
            self.assertIn(("tool_call", "missing_step"), missing)
            self.assertIn(("tool_result", "missing_step"), missing)
            pieces = {g["piece"] for g in groups}
            self.assertIn("messages[#].tool", pieces)  # the model now sees a refusal as the tool result


class RecordRoundTripTests(unittest.TestCase):
    """Recorder replay file -> `record scenario` -> a scenario that replays to the same checkpoints."""

    def test_replay_file_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="hermes-parity-record-") as d:
            tmp = Path(d)
            src = harness.load_scenarios(FIX / "scenarios" / "seeded")[0]
            replay = tmp / "owner-replay.jsonl"
            first = harness.run_one(FAKE, sys.executable, src, "B", tmp / "homes", "rec",
                                    extra_env={"PARITY_REPLAY_OUT": str(replay)})
            self.assertEqual(first.status, "ok")
            types = [json.loads(line)["type"] for line in replay.read_text().splitlines()]
            self.assertEqual(types, ["user", "reply", "tool_result", "reply", "tool_result", "reply"])
            with redirect_stdout(io.StringIO()):
                cli.main(["record", "scenario", str(replay), str(tmp / "scen" / "recorded"), "--toolsets", "file",
                          "--model", "synthetic/model"])
            again = harness.load_scenarios(tmp / "scen")[0]
            second = harness.run_one(FAKE, sys.executable, again, "B", tmp / "homes", "replayed")
            diffs = differ.diff(first.records, second.records, old_raw=first.raw, new_raw=second.raw)
            self.assertEqual(diffs, [])


@unittest.skipUnless(os.environ.get("PARITY_REAL_OLD_TREE") and os.environ.get("PARITY_REAL_OLD_PYTHON"),
                     "set PARITY_REAL_OLD_TREE and PARITY_REAL_OLD_PYTHON to run the real-tree seeded check")
class RealTreeSeededTests(unittest.TestCase):
    """Opt-in: the same three seeds injected into a copy of a REAL Hermes tree."""

    def test_real_tree_three_fingerprints(self):
        import subprocess
        tree, py = Path(os.environ["PARITY_REAL_OLD_TREE"]), os.environ["PARITY_REAL_OLD_PYTHON"]
        with tempfile.TemporaryDirectory(prefix="hermes-parity-real-") as d:
            tmp = Path(d)
            subprocess.run([sys.executable, str(REPO / "scripts" / "seed_real_tree.py"), str(tree),
                            str(tmp / "seeded")], check=True, capture_output=True)
            meta = harness.run(tree, tmp / "seeded", REPO / "scenarios", tmp / "run", old_python=py, new_python=py,
                               modes=("A",), scratch=tmp / "homes", log=lambda _s: None)
            self.assertTrue(all(r["old_status"] == r["new_status"] == "ok" for r in meta["runs"]))
            groups = report.write(tmp / "run")["report"]["groups"]
            self.assertEqual({g["piece"] for g in groups},
                             {"tools[read_file].schema", "system_prompt.section[#]", "tools[search_files].schema"})


class PluginManifestTests(unittest.TestCase):
    def test_hooks_are_observer_only(self):
        plugin = load_module(harness.PLUGIN_DIR / "__init__.py", "parity_recorder_obs")
        registered = {}

        class Ctx:
            def register_hook(self, name, fn):
                registered[name] = fn

        plugin.register(Ctx())
        self.assertEqual(set(registered), {"pre_api_request", "post_api_request", "pre_tool_call", "post_tool_call",
                                           "pre_llm_call"})
        with tempfile.TemporaryDirectory() as d:
            import os
            os.environ["PARITY_RECORDER_OUT"] = str(Path(d) / "c.jsonl")
            try:
                # Returns None even for a modify-shaped payload, and swallows a broken payload.
                self.assertIsNone(registered["pre_tool_call"](tool_name="t", args={"x": 1}, turn_id="t1"))
                self.assertIsNone(registered["post_tool_call"](result=object(), turn_id="t1"))
                self.assertIsNone(registered["pre_llm_call"](user_message="hi", turn_id="t1"))
            finally:
                del os.environ["PARITY_RECORDER_OUT"]

    def test_manifest_lists_registered_hooks(self):
        text = (harness.PLUGIN_DIR / "plugin.yaml").read_text()
        for hook in ("pre_api_request", "post_api_request", "pre_tool_call", "post_tool_call", "pre_llm_call"):
            self.assertIn(f"  - {hook}", text)


if __name__ == "__main__":
    unittest.main()
