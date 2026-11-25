# hermes-parity: specification (v0)

Specification only, not dataset input. No client data anywhere in this repo or its fixtures.

## Problem
Before updating Hermes Agent (old tree -> new tree), the owner wants proof of what changes in how agents ACT, not just that tests pass. LLM output varies run to run, so classic shadow-diff tools (GitHub Scientist, Diffy) don't work: they treat varying output as noise and hide real bugs along with it. Anthropic's method (spec/anthropic-rust-migration-post.md): run the model ONCE and feed the same model output to both versions; compare at checkpoints; keep content-free difference records; group them by fingerprint; humans gate fixes.

## v0 scope
1. Recorder (Hermes plugin, observer-only, fails open). It uses the documented observer hooks (spec/hermes-observer-hooks.md): pre_api_request / post_api_request / pre_tool_call / post_tool_call, keyed by turn_id and tool_call_id. It writes one JSONL checkpoint per step: kind, tool name, a canonical hash and length of the args and of the result, status, and the stable shape (sorted key paths). Raw text is OFF by default. It records the model replies needed for replay into a separate local file the owner controls; that file never enters reports.
2. Two-tree harness. It runs the same scenario through the OLD and NEW Hermes trees (separate checkouts; PYTHONPATH or each tree's own venv) with:
   - a scripted model: replays recorded model replies, so no network and no API cost, and the model "runs once";
   - stubbed tools: return recorded results, so nothing with side effects ever fires;
   - an isolated HERMES_HOME per run under a scratch dir.
   Mode A, input parity: build the request (system prompt, tool schemas, message list) in each tree and compare, with no model call. Mode B, decision parity: feed the same recorded model reply through both trees and compare the tool calls dispatched, their args, the order, and the final message shape.
3. Differ. It aligns checkpoints by (turn, step index, kind) and emits, per difference: step, piece (e.g. system_prompt.section[3], tools[name].schema, tool_call[2].args), first-diff byte offset in the canonical serialization, length delta, and the old/new hashes. NO content. It also reports missing or extra steps.
4. Grouper. Fingerprint = hash(step kind, piece path with indices normalized, offset bucket, length-delta sign/bucket). It outputs a count table per fingerprint and flags any fingerprint not seen in a baseline file ("new kind of difference"). A plain-English summary line per group says what changed (e.g. "tool schema for X changed: +34 bytes from byte 120").
5. CLI: `hermes-parity record ...` (or plugin config), `hermes-parity run --old <tree> --new <tree> --scenarios <dir>`, `hermes-parity report <run>` printing a markdown table plus a JSON file.

## Acceptance (v0)
- Seeded diffs: build synthetic scenario fixtures with three known differences injected via a patched copy (key-order change in a tool schema, a stripped trailing newline in a prompt section, a missing tool description). They must come out as exactly three fingerprints with correct pieces and offsets. A clean pair (same tree twice) must give zero differences.
- Privacy test: a fixture containing a unique canary string in a user message, tool args and a tool result; the canary must appear in NO report, record or log output (the scripted-replay file is exempt and is the owner's).
- Real run: run Mode A and Mode B on the OLD tree (caafe5389ae5, the installed v0.23.3) vs the NEW tree (8fc037444731, stable v0.23.4) using synthetic scenarios only: (a) plain chat, (b) one read-only tool call, (c) two tool calls in sequence, (d) a refused tool call. Report the fingerprint table. This is the first real result; say plainly whatever it did or didn't cover.
- Stdlib only (PyYAML optional). MIT. Tests run with `python3 -m unittest`. `hermes plugins validate` passes for the recorder plugin part.
- README in plain language: what it proves, what it does NOT prove (wording quality, live side effects, providers), a "Known limits" list, and credit to Anthropic's post as the method source (link). Name collision note: the existing catalog plugin `shadow` (salch-cred) compares two models' answers and is a different tool; don't call ours "shadow".

## Out of scope for v0
Live traffic tee; auto-fix agents; non-Hermes agents; any network or model call.
