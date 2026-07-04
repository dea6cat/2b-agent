# atomic-agent → 2B: speed & reliability — what to take, what not, and a roadmap

**Date:** 2026-07-03
**Source analyzed:** `/Users/do519-lap/repo_apps/atomic-agent` (AtomicBot-ai — TypeScript/Node, ~1100 TS files, local-first, **llama.cpp-first** via a raw `llama-server` `/completion` endpoint, ink TUI, GBNF-constrained tool calls, memory/intent/skills fabrics, MCP, HTTP + Tauri sidecar).
**Why we looked:** it publishes a **GAIA validation Level-1** benchmark (53 tasks, same local `qwen-3.6-35b`) claiming **69.8% vs 58.5%** accuracy and **~217 s vs ~351 s / task** against the "Hermes" agent (NousResearch) — i.e. "+11.3pp, ~1.6× faster, same model." If the *agent loop* alone buys that, it's worth mining.
**Filter applied:** 2B's thesis — *frozen 5-tool schema, all complexity host-side, native Ollama `/api/chat` (no shim), local-first, small-model reliability, stdlib-heavy, macOS-first.* Anything that widens the model's world or bolts on a service is rejected unless it lives entirely host-side.

---

## The one-paragraph verdict

**The speed story is real and portable; the accuracy story is soft; and 2B is already ahead on UX.** The ~1.6× wall-clock edge is driven by a handful of **host-side loop mechanics** — parallel read batching, a byte-stable prompt prefix, and early loop cutoff — every one of which fits 2B's thesis and reaches Ollama fine. The **+11.3pp accuracy claim does not survive scrutiny**: it's a single un-averaged n=53 run whose 6-task gap is **not statistically significant** (McNemar p≈0.3), the opponent ran with its **skills disabled** (`--ignore-rules`) while atomic-agent ran fully featured (memory + embeddings on), and temperature was uncontrolled in a direction that favors atomic. The genuinely differentiating techniques (**GBNF constrained decoding, Turboquant KV-cache quant, slot pinning**) are **llama.cpp-native and unreachable through Ollama** without abandoning the no-shim thesis — so we take the *robustness layer wrapped around* the grammar, not the grammar. Net: adopt the loop mechanics, decline the llama.cpp plumbing, and note that 2B already beats atomic-agent on diff confirmation, per-session grants, @-file completion, notifications, and compaction quality.

---

## A. What to TAKE (host-side, fits the thesis) — ranked by impact × fit

| # | Take | Why it helps | Reaches Ollama? |
|---|---|---|---|
| A1 | **Host-side tool-call robustness layer** — lenient arg-coercion parser (accept `tool`/`name`/`action`, `args`/`arguments`, stringified-JSON args), pre-parse defect gating (`done_reason` truncated/empty → don't re-prompt the same wall), **one bounded repair retry** fed the exact validation error, few-shot per-tool examples, conservative sampling (`temperature≈0.2`, `repeat_penalty≈1.1`). | Directly raises small-model tool-call success without a schema change. Extends 2B's existing `_missing_required`. | Yes — pure host-side. |
| A2 | **Never-throw turn closure** — every exit path (max-turns, tool error, model error) emits a *final assistant message*, never a stack trace or empty output. | GAIA-style grading needs a parseable answer; an agent that throws mid-task guarantees a miss. Cheap accuracy floor. | Yes. |
| A3 | **Parallel read batching** — Ollama already returns a `tool_calls` array; fan the **read-class** calls (`list_files`, `read_file`, `search_files`, read-only `run_git`/`run_command`) out concurrently host-side, serialize mutating calls, keep them behind confirmation. | The single biggest *measured* speed lever (their own note: 11 min → 5 min on a multi-file scan). | Yes — native array + a host-side gather. |
| A4 | **Loop-guard upgrade** — graduated warn→veto→breaker ladder; hash tool *results* with volatile fields (timestamps/ids/sizes) stripped so "identical-but-for-a-timestamp" still trips; (if web/fetch ever lands) a "wandering" distinct-args detector. | Recovers stalled tasks (accuracy) and caps token/step burn on doomed loops (speed). Straight upgrade to 2B's loop-guard. | Yes. |
| A5 | **Byte-stable prompt prefix + tail-placement** — keep system prompt + frozen tool schema byte-identical across a session; push *all* volatile content (date, context meter, injected files, stale warnings) to the tail; place the **date + final directive next to the generation point**. | Protects Ollama's own prefix KV cache = free per-turn latency; tail-placed date fixes small-model year-anchoring/repetition loops. | Yes — implicit prefix cache + `keep_alive`. |
| A6 | **Aged tool-result capping** — render a `read_file`/`search_files` result *in full only on the step that consumes it*, then shrink it in history. | Cuts long-context bloat from large tool outputs that otherwise sit in every later prompt. | Yes. |
| A7 | **Shell-command guard tiers** — a static classifier *under* the existing per-session grants: **hardline-block** (`rm -rf /`, `rm -rf ~`, `mkfs`, `dd of=/dev/…`, fork bombs, `shutdown`) that survives even accept-edits/`--yolo`, and **safe-allow** (`whoami`, `pwd`, `--version` probes, `which`) that never prompts. Fail-closed default. | Cuts prompt fatigue *and* adds a real safety floor. 2B has grants but no static classification layer. | Yes — regex/set logic. |
| A8 | **Eval harness hardening** — port the official exact-match scorer (number/list/string normalization, no LLM judge); add **bootstrap confidence intervals + a McNemar paired test**; multi-seed runs; a **git-dirty-gated environment snapshot**; per-case temp workspace; sequential execution against the local backend. | Turns "we scored X%" into "X% [CI a–b]" — the exact rigor their headline lacked, and a credibility differentiator. | Yes — stdlib. |
| A9 | **Project-instructions injection** — one optional read of a project-root `CLAUDE.md`/`AGENTS.md` into a **fixed prompt slot** (the tail, per A5). Dumb: one file, read once per run, no gating/versioning. | ~15 lines; gives the model repo conventions → higher coding accuracy. | Yes. |
| A10 | **Effective-cap budgeting + true-window probe** — conversation cap = `window − fixed section costs − completion reserve − safety margin` (not a flat 75%); read the running model's real window from Ollama `POST /api/show` (`model_info.*.context_length`) as ground truth vs. the catalog. | Prevents silent overflow when a Modelfile `num_ctx` < architecture max. | Yes. |
| A11 | **Prompt-drift replay** — hash the assembled system+tool prefix into each session record; a `2b trace replay` recomputes with current code and flags drift. | A cheap guardrail that literally asserts the *frozen 5-tool schema didn't move* between versions. | Yes. |
| A12 | **TUI micro-polish** — elapsed timer on the spinner line (spot stuck runs), Esc snaps scroll-to-bottom *before* aborting, double-Ctrl-C armed window (first aborts, second within ~1.5 s quits). | Small robustness/clarity wins over 2B's already-good TUI. | Yes. |

---

## B. What NOT to take (and why)

| Not taking | Why |
|---|---|
| **GBNF grammar-constrained decoding** (their flagship "proper tool calling") | Not reachable via Ollama native tool-calling. Ollama's `format` param constrains message **content**, not `tool_calls`; forcing it tends to suppress the tool-call channel. Matching them means building the host-side shim 2B's thesis rejects. *Optional experiment only:* a `format`-as-JSON-schema **fallback** invoked only when a small model returns malformed/absent `tool_calls` — a targeted net, not the primary path. |
| **"Turboquant"** | It's not a compression algorithm — it's a `--cache-type-k/-v turbo3` KV-cache quant in their **private llama.cpp fork binary**. 2B talks to Ollama over HTTP and never launches llama.cpp. Closest analog is *documenting* `OLLAMA_KV_CACHE_TYPE=q8_0` + `OLLAMA_FLASH_ATTENTION=1` on the Ollama host — a deployment note, not code. |
| **SlotManager / `slot_id` / `cache_prompt` pinning** | Ollama `/api/chat` exposes no slot API. The benefit (prefix-cache reuse) is obtained implicitly via A5 + a warm `keep_alive`. |
| **Memory Fabric / Intent Fabric / Skills registry + hub** | Long-lived-*assistant* scope (SQLite + FTS5 + embeddings + link graph + proactivity). Violates 2B's YAGNI/stdlib thesis with zero coding-agent payoff. (Intent Fabric isn't even built — it's a planning doc.) The one nugget is A9. |
| **Frequent/rare tool tiering + `tool.view`** | Exists only to manage a 63-tool surface. 2B's frozen 5-tool schema already solves "minimize what the small model sees" more aggressively — this is a *validation* of the thesis, not a gap. |
| **HTTP server / OpenAI-compat endpoint / Tauri sidecar** | Off the CLI-first, no-embedding thesis. |
| **Node SEA single-file packaging** | Mach-O re-signing + notarization + Node-version pinning pain that exists only because it's a Node app. `uv tool install` + the shell installer sidesteps all of it. |
| **TurnController multi-session primitive / cross-session state** | 2B's worker-thread model is a different (heavier) choice; their single-thread-async + synchronous-sqlite design is a *data point* to weigh, not code to port. Their global mutable memory is why they needed a stateless eval flag — 2B's no-memory design is a reproducibility **asset**. |
| **Model-summarized compaction** | 2B **already wins** here — 2B model-summarizes dropped history; atomic-agent only emits a one-line bookkeeping recap. (Consider borrowing their deterministic one-liner as a *fast fallback* when the summarizer call is slow/unavailable.) |

**Already ahead / at parity (no action):** inline line-numbered diff confirmation (theirs clips to 240 chars in a banner), per-session "allow" grants (they prompt every call), @-file completion (they lack it), desktop finish notifications (they lack them), live context meter, RAM-aware window sizing + the per-model cloud catalog.

---

## C. Roadmap (phased, host-side, each independently shippable & unit-testable)

Ordered by ROI. P1–P4 are the reliability/speed core and the highest-value work; the rest is polish, safety, and eval rigor.

### Phase P1 — Tool-call robustness layer (A1)
- **Spec:** In the dispatch path, before rejecting a tool call: (1) coerce arg shape (accept `tool`/`name`/`action`, `args`/`arguments`, re-parse stringified-JSON args); (2) if the model returned no/малformed call and `done_reason` ∈ {length, truncated} or content is empty, don't re-prompt identically — surface a bounded error; (3) on a validation failure, do **one** repair round-trip that feeds the exact reason back, capped (~1024 tokens) so reasoning models don't self-deliberate. Add a few-shot example per frozen tool to the system prompt; set default `options.temperature≈0.2`, `repeat_penalty≈1.1`.
- **Files:** `orchestrator.py` (dispatch/repair), `tools.py` (coercion helper), `providers/*` (sampling defaults), `toolspec.py` (few-shot examples). New `tests/test_toolcall_repair.py`.
- **Effort:** M. **Note:** builds on the existing `_missing_required`; highest thesis-fit item.

### Phase P2 — Never-throw turn closure (A2)
- **Spec:** Guarantee `run_task` always ends with a final assistant message. Classify terminal reasons (`reply`/`max_turns`/`cancelled`/`failed`); on any caught exception, emit a synthetic "(stopped: …)" assistant message and mark the task, rather than surfacing a trace or empty output.
- **Files:** `orchestrator.py` (`run_task` terminal handling). Extend `tests/test_loop_guard.py` or new `tests/test_turn_closure.py`.
- **Effort:** S. Pairs with P1.

### Phase P3 — Parallel read batching (A3)
- **Spec:** When Ollama returns multiple `tool_calls`, partition by a static read/mutate class; execute the read-class group concurrently (thread pool / `asyncio.gather` equivalent in the worker), preserve result order, serialize mutating calls and keep each behind its confirmation. Add prompt guidance that a solo call is valid (counter the first-token bias toward single calls) and that independent reads may be batched.
- **Files:** `orchestrator.py` (`_dispatch_tool` batching), a small resource-class map, `tools.py`. New `tests/test_parallel_reads.py`.
- **Effort:** M. **Note:** the biggest measured speed lever; frozen schema maps cleanly (5 reads vs 2 mutators + delegate).

### Phase P4 — Loop-guard upgrade (A4)
- **Spec:** Extend the existing loop-guard to a warn(≥3)→veto(≥5, substitute a corrective synthetic result)→breaker(≥3 vetoes → graceful final reply) ladder; compute the no-progress signature over tool *results* with volatile fields stripped (timestamps, ids, byte sizes). Hardcode sane thresholds (no env-var sprawl — simplicity thesis).
- **Files:** the loop-guard module + `orchestrator.py`. Extend `tests/test_loop_guard.py`.
- **Effort:** M.

### Phase P5 — Prompt-prefix stability + tail placement (A5)
- **Spec:** Audit prompt assembly so the system prompt + frozen tool schema are byte-identical within a session; move date, context-meter text, injected files, and stale-file warnings into a tail block; render the date + "respond now" directive adjacent to the generation point. Set/verify Ollama `keep_alive`. Add a test asserting the prefix is stable across turns given fixed inputs.
- **Files:** wherever the system prompt/messages are assembled (`orchestrator.py`), `providers/ollama.py` (`keep_alive`). New `tests/test_prompt_prefix.py`.
- **Effort:** S–M. Mostly "don't break it" + verification.

### Phase P6 — Context budgeting refinements (A6, A10)
- **Spec:** (a) Aged tool-result capping: keep the full body of a read/search result only within its current turn, then cap it (~a few KB) in history. (b) Compute the conversation cap by subtracting fixed section costs + a completion reserve + a safety margin from the true window, instead of a flat fraction. (c) Read `POST /api/show` `context_length` and clamp to `min(catalog/num_ctx, server-reported)`.
- **Files:** `orchestrator.py` (`context_budget`, compaction), `providers/ollama.py` (`/api/show` probe). Extend `tests/test_context_meter.py` / `tests/test_catalog.py`.
- **Effort:** M. Partly overlaps what 2B already does.

### Phase P7 — Shell-command guard tiers (A7)
- **Spec:** A pure-function classifier returning `allow` / `confirm` / `block`, consulted in `run_command`/`_run_git` *before* the grant/confirm check. Hardline-block list survives accept-edits and any bypass; safe-allow list skips confirmation; everything else → existing confirm/grant flow. Metachar-aware (a `--version` probe auto-allows only with no `;|&$` etc.). Fail-closed.
- **Files:** new `cmdguard.py` (dep-free, testable), wired in `orchestrator.py`. New `tests/test_cmdguard.py`.
- **Effort:** S–M. Safety + less prompt fatigue.

### Phase P8 — Project-instructions injection (A9)
- **Spec:** On task start, if a project-root `CLAUDE.md` (fallback `AGENTS.md`) exists, read it once and inject verbatim into the tail slot from P5, under a `### project instructions` header, size-capped. No keywords, no watching, no state.
- **Files:** `orchestrator.py` (prompt assembly), a small reader in `tools.py`/`config.py`. New `tests/test_project_instructions.py`.
- **Effort:** S.

### Phase P9 — Eval harness hardening (A8)
- **Spec:** (a) Port the official exact-match scorer (strip `$ % ,`; list split on `,`/`;` with per-element compare + length check; punctuation/space-insensitive scalar compare) — no LLM judge. (b) Add bootstrap CIs (seeded RNG) and a McNemar paired test over per-case pass/fail. (c) Multi-seed (N≥3) per case with variance reported. (d) Environment snapshot capturing git SHA + dirty-file count that *refuses to publish from a dirty tree*, plus the actually-used sampling params. (e) Per-case `mkdtemp` workspace, deleted after traces are copied out; sequential execution against the local backend.
- **Files:** extend the existing eval harness modules; new scorer + stats module. Tests for the scorer + stats.
- **Effort:** M. Supports 2B's honest/reproducible positioning — and lets 2B publish CI-bounded numbers the surveyed benchmark couldn't.

### Phase P10 — Prompt-drift replay (A11)
- **Spec:** Store a salted hash of the assembled prefix in each session record; add `2b trace replay <session>` that rebuilds the prefix with current code and reports `drift: true/false` per turn. No LLM re-run.
- **Files:** `persist.py` (store hash), `cli.py`/`commands.py` (`trace replay`). New `tests/test_drift_replay.py`.
- **Effort:** S.

### Phase P11 — TUI micro-polish (A12)
- **Spec:** Elapsed timer on the running-tool line; Esc snaps scrollback to bottom before it aborts; double-Ctrl-C armed window (first press aborts + arms ~1.5 s, second quits).
- **Files:** `app_tui.py`.
- **Effort:** S.

### Phase P12 — (stretch, differentiator) macOS `sandbox-exec` seatbelt
- **Spec:** Optionally wrap `run_command` in a `sandbox-exec` profile confining writes to the workspace root. atomic-agent has **no** OS-level sandbox — shipping this puts 2B *ahead* on safety, not just at parity. Opt-in; degrade gracefully where unavailable.
- **Files:** new `seatbelt.py`, wired in `tools.do_run_command`. Tests gated on Darwin.
- **Effort:** M–L. Off the critical path; a genuine lead if pursued.

---

## Suggested order

1. **P1 + P2** — tool-call robustness + never-throw closure. Highest thesis-fit, pure host-side, directly raises small-model reliability (the real accuracy lever, vs the grammar we can't take).
2. **P3** — parallel read batching. The biggest *speed* win; where atomic-agent's ~1.6× mostly comes from.
3. **P4** — loop-guard upgrade. Recovers stalls, caps runaway burn.
4. **P5 + P6** — prefix stability + context budgeting. Free latency (prefix cache) + long-context correctness.
5. **P7** — shell-command guard tiers. Safety floor + less prompt fatigue.
6. **P8, P9** — project instructions + eval rigor. Coding accuracy + a credibility edge over the surveyed benchmark.
7. **P10, P11** — drift replay + TUI polish.
8. **P12** — seatbelt sandbox, if we want to lead on safety.

**Honest footnote on the benchmark that prompted this.** The +11.3pp accuracy is within single-run noise (McNemar p≈0.3) and was measured with the opponent's skills disabled and its temperature uncontrolled; treat it as *not established*. The ~1.6× speed edge is more believable but partly reflects design differences, not pure loop efficiency. The right takeaway isn't "we're losing" — it's that the **loop mechanics above are real, portable, and on-thesis**, and that 2B can out-*rigor* the comparison by reporting CI-bounded numbers (P9).
