# hermes-parity

Before you update Hermes Agent, this tool shows what changes in how the agent **acts**, not
just whether the test suite passes.

It runs the same conversation through the OLD Hermes tree and the NEW one, feeds both the exact
same model replies, and compares what each version built and did, step by step. It never stores
or prints the text of the conversation. It keeps only hashes, lengths and where the bytes first
differ, and it groups the differences by "fingerprint" so one bug shows up as one row instead of
hundreds.

The method is Anthropic's, from their write-up on moving the Messages API from Python to Rust:
run the model once, replay its output to both versions, compare at checkpoints, keep difference
records with no content, group them by fingerprint, and let people decide on fixes.
Source: <https://www.anthropic.com/research/migrating-api-to-rust>.
Spec for this repo: `spec/SPEC.md`.

Name note: the Hermes catalog already has a plugin called `shadow` (salch-cred). It compares the
answers of two *models*. This tool compares two *Hermes versions*, and it is not called "shadow".

## What it proves

- **Input parity (Mode A).** For each scenario, both trees build the first model request: system
  prompt (split into its markdown sections), every tool schema, tool order, the message list and
  request parameters. Any byte that differs is reported with the piece, the first differing byte,
  the length change and both hashes.
- **Decision parity (Mode B).** The whole turn runs on the same scripted replies. It compares every
  request each tree builds as the turn goes on, which tools get dispatched and in what order, the
  arguments as Hermes passed them on, each tool's status and result as Hermes saw it, and the
  final message shape (role sequence, tool calls, completion).
- **No side effects, no cost.** The model is a local scripted server on 127.0.0.1 that replays
  recorded replies. Every tool handler is swapped for a stub that returns the recorded result. All
  other sockets are refused. Each run gets a fresh HOME and HERMES_HOME in a scratch dir.

## What it does NOT prove

- **Wording quality.** It can tell you a prompt section changed by 34 bytes at byte 120. It cannot
  tell you whether the new wording makes the model behave better or worse. Only a live-model eval
  can answer that.
- **Live side effects.** Tools are stubbed, so a change inside a tool's own code (how `read_file`
  reads a disk, how `terminal` runs a command) is invisible. What it sees is what Hermes sends to
  the tool and what Hermes does with the result.
- **Providers.** The scripted model speaks OpenAI chat-completions through Hermes' `custom`
  provider. Anthropic-native, Codex/Responses, Bedrock and other transports, plus provider-specific
  request shaping, are not exercised.
- **How a real model would react to a changed input.** The replies are fixed. If NEW changes the
  prompt, a real model might answer differently, and that is beyond what this tool can see. That is
  the whole point of "run the model once": it separates "Hermes changed" from "the model varied".

## How to use

Stdlib Python 3.11+ only. Each Hermes tree needs its own Python env with that tree's
dependencies. The tree goes on PYTHONPATH and is never installed into or edited:

```sh
# one env per tree (example with uv; Hermes pins its deps for Python 3.14)
UV_PROJECT_ENVIRONMENT=$SCRATCH/venv-old uv sync --frozen --no-install-project --python 3.14 --project trees/old
UV_PROJECT_ENVIRONMENT=$SCRATCH/venv-new uv sync --frozen --no-install-project --python 3.14 --project trees/new

bin/hermes-parity run --old trees/old --new trees/new \
    --old-python $SCRATCH/venv-old/bin/python --new-python $SCRATCH/venv-new/bin/python \
    --scenarios scenarios --out runs/my-run --scratch $SCRATCH/homes
bin/hermes-parity report runs/my-run                       # markdown table + report.json
bin/hermes-parity report runs/my-run --write-baseline base.json
bin/hermes-parity report runs/next  --baseline base.json   # flags fingerprints not seen before
```

A run dir holds `run.json` (trees, SHAs, per-run status), `checkpoints/*.jsonl`, `diffs.jsonl`,
`report.md` and `report.json`. None of them contain conversation text.

### Scenarios

A scenario is a folder with:

- `scenario.json`: setup only (platform, model name, `enabled_toolsets`; `"platform"` means the
  stock toolsets for that platform).
- `replay.jsonl`: the owner's replay file. It holds user messages, the model replies to replay and
  the recorded tool results. It is raw text, so it stays private, and nothing from it reaches a report.

The four in `scenarios/` are synthetic: (a) plain chat on the stock cli tool surface, (b) one
`read_file`, (c) `search_files` then `read_file`, (d) a `terminal` call that Hermes must refuse
because only the `file` toolset is enabled.

### Recorder plugin

`recorder_plugin/hermes-parity-recorder` is an observer-only Hermes plugin. It uses the documented
hooks `pre_api_request`, `post_api_request`, `pre_tool_call` and `post_tool_call`, plus
`pre_llm_call` for the optional replay file. Every hook returns None and swallows its own errors.
It writes one content-free JSONL checkpoint per step. Raw text is off. Two opt-in channels exist,
and both belong to the owner:

- `PARITY_REPLAY_OUT=<file>` writes the replies and tool results needed to replay a session.
  `hermes-parity record scenario <file> <dir>` turns that file into a scenario.
- `PARITY_SINK=<loopback url>` is set by the harness. It lets the recorder hand raw piece bytes to
  the harness's memory so the harness can find the first differing byte. Non-loopback URLs are refused.

`hermes-parity record install --home <HERMES_HOME>` copies it into a home. It refuses the live
`~/.hermes` unless you pass `--i-mean-it`. `hermes plugins validate` passes on both v0.23.3 and v0.23.4.

## How the comparison works

- **Canonical form.** Compact JSON that keeps key order, because a key-order change moves the bytes
  the model reads and breaks the prompt-cache prefix. Strings are compared as UTF-8 bytes.
  Offsets are byte offsets.
- **Checkpoint.** `(turn, kind, step)` plus pieces `{piece, hash, len, shape}`. `step` counts per kind,
  so one extra tool call shifts only the tool steps. `shape` is the sorted key paths. Keys that could
  be data (every key of a tool result, deep keys of args and messages, anything not identifier-shaped)
  are replaced by a short hash.
- **Difference record.** Scenario, mode, turn, kind, step, piece (for example
  `system_prompt.section[3]`, `tools[read_file].schema`, `tool_call.args`), first-diff byte offset,
  old and new length, delta, old and new hash, and key paths added or removed. Missing or extra
  steps and pieces count too.
- **Fingerprint.** `sha256(kind | piece with indices normalized | offset bucket | delta sign/bucket
  | change type)`. Buckets are powers of two. Each group gets a one-line summary such as
  `tool schema for search_files changed: -357 bytes from byte 24, key paths +0/-1`.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

- Seeded diffs. A patched copy of a small fake Hermes tree carries three known changes (key order
  in the `read_file` schema, a stripped trailing newline in a prompt section, a missing
  `search_files` description). It must give exactly three fingerprints with the right pieces, byte
  offsets and deltas, worked out independently from the schemas.
- Clean pair. The same tree twice must give zero differences.
- Privacy canary. A unique string sits in the user message, in tool args and in a tool result
  (as a value and as a key). It must not appear in stdout, stderr, any file in the run dir, or the
  recorder's own checkpoint files.
- Decision parity. A tree that refuses one tool must show the dispatch as a missing step in Mode B
  only.
- Opt-in, real tree: `PARITY_REAL_OLD_TREE=<tree> PARITY_REAL_OLD_PYTHON=<python>` seeds the same
  three changes into a copy of real Hermes (`scripts/seed_real_tree.py`) and expects three fingerprints.

## Results

- `results/2026-11-25-v0.23.3-vs-v0.23.4/`: OLD `caafe5389ae5` (v0.23.3) vs NEW `8fc037444731`
  (v0.23.4), Modes A and B, the four synthetic scenarios. **Zero differences.** The project's own
  committed prompt snapshot (`tests/fixtures/prompt_surface/`) is also byte-identical between the
  two SHAs, which agrees with Mode A.
- `results/2026-11-25-v0.23.3-vs-seeded-copy/`: OLD vs a copy of OLD with the three seeds. Exactly three
  fingerprints, 12 records each, at byte offsets 368, 783 and 24. Each offset was checked by hand
  against the real constants.

## Known limits

1. **Stubbed tools hide tool internals.** A change inside a tool handler is invisible, by design.
   Agent-level tools that Hermes runs inline (`todo`, `memory`, ...) skip the registry, so they are
   not stubbed. The shipped scenarios do not call them, and scenarios should keep it that way.
2. **One transport.** Only chat-completions through the `custom` provider, streaming as Hermes
   chooses. Other provider paths are not covered.
3. **Pinned environment.** Clock, tool availability (`check_fn` forced true), host-probe lines and
   docker detection are pinned the same way as Hermes' own `scripts/ci/prompt_surface.py`. Any pin
   a tree lacks is listed as a driver note in the report. A difference that only shows up on a
   real host or a real config will not appear.
4. **Offsets need the live pair.** Exact byte offsets come from raw bytes the harness holds in memory
   during the pair run. Comparing two stored checkpoint files offline gives hashes and lengths but
   `offset: na`.
5. **Hash privacy is not encryption.** The hashes are unsalted and truncated. A short, guessable
   value could be confirmed by someone who already suspects it. Shape keys are hashed for data
   pieces, but piece names include tool names, and a model reply could invent a tool name.
6. **Single-turn scenarios.** The harness supports several user messages, but the shipped scenarios
   are single-turn, and compression, `/steer`, subagents and approvals are not exercised.
7. **Scripted replies are fixed.** If NEW sends a different request, the replay still gives the
   recorded reply. That is right for "same model output to both", but it does not model how a
   real model would react.
8. **Per-run homes hold scenario text** (Hermes' own session state, the driver job file). They live
   in the scratch dir and are deleted after each run unless `--keep-homes` is set.
9. **Trees are not fully untouched on disk.** No tracked file changes, and bytecode writes are off,
   but Hermes writes a git-ignored `.bytecode-fingerprint` at the tree root the first time it is
   imported. Delete it afterwards if the tree has to stay pristine.
10. **Recorder replay file and sessions.** `PARITY_REPLAY_OUT` records whatever the session contains.
   Use it only on sessions whose contents you own. Never point it at client conversations.

## License

MIT (see `LICENSE`).
