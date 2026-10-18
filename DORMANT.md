# Project status: dormant

**As of 2026-09-30, 2B is dormant.** Development has moved to [ClydeCLI](https://github.com/dea6cat/ClydeCLI), and there is one maintainer, so the older project does not get worked on in parallel — it gets replaced.

## What "dormant" means, plainly

- **No maintenance.** No new model families are being tested against the frozen tool surface. A local-first agent is a compatibility promise against a fast-moving ecosystem (new Qwen, gpt-oss, Nemotron releases; Ollama updates); that promise decays without continuous validation, and none is happening here.
- **Issues and PRs are not being monitored.**
- **The license is unchanged:** PolyForm Noncommercial 1.0.0 (see LICENSE). Copies from before the July 2026 relicense remain Apache-2.0, as the license history states.
- **The code stays up, as reference material.** Nothing will be deleted.

## Why 2B is still worth reading

2B exists to prove one thesis: *small local models fail as agents because of harness complexity, not model weakness* — the generic OpenAI-compatible shim degrades their tool use, and a big model-facing surface buries what capability they have. The evidence for that thesis lives in this codebase:

- **The frozen tool surface.** `src/two_b/tooling/tools.py` carries the schema that made small models reliable, ported verbatim from the validated prototype and deliberately kept minimal (list, read, search, edit, write, run). All other complexity — the TUI, the plan checklist, the command guard, the seatbelt sandbox, the untrusted-output handling — lives on the host side, around the loop, never in the model's world.
- **Native transport.** `providers/ollama.py` speaks Ollama's own `/api/chat`; cloud providers get the shim they can tolerate, local ones never do.
- **A verified restructure.** The flat 50-module package was regrouped into 7 role-based subpackages with identical test results before and after (896 tests, the same 3 pre-existing failures), validated against a 3,122-node code graph. The layout you see is the output of that exercise.

The thesis, and the lessons, moved forward: ClydeCLI's deferred-tool layer (`src/tool_system/deferral.py`) applies the same idea inside a maximalist harness — 56 tools for whoever can use them, a small per-turn surface for whoever can't.

**If you want a tool to use today, use ClydeCLI.** If you want to study how a small-model harness should be built, read this repo.
