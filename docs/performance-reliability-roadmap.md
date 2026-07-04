# Local-agent survey → 2B: speed & reliability — what to take, what not, and a roadmap

**Date:** 2026-07-03
**Sources analyzed:**
- **Part I** — `/Users/do519-lap/repo_apps/atomic-agent` (AtomicBot-ai — TypeScript/Node, ~1100 TS files, local-first, **llama.cpp-first** via a raw `llama-server` `/completion` endpoint, ink TUI, GBNF-constrained tool calls, memory/intent/skills fabrics, MCP, HTTP + Tauri sidecar).
- **Part II** — `/Users/do519-lap/repo_apps/loom` (Python, ~500 files, local-ready LLM *execution harness*: decompose → dependency-graph → parallel subtasks → independent verification → replan, model routing, fuzzy edit tool, lossless SQLite memory + recall tool, Textual TUI, REST/MCP). Same "the harness drives, not the model" thesis as 2B — and being Python, its code is directly liftable.
**Why we looked:** atomic-agent publishes a **GAIA Level-1** benchmark (53 tasks, same local model) claiming **+11.3pp accuracy, ~1.6× faster** vs the "Hermes" agent (NousResearch); Loom markets fuzzy edits, lossless memory, and harness-driven verification as its edge. If the *agent loop / host-side machinery* alone buys reliability on small models, it's worth mining.
**Filter applied:** 2B's thesis — *frozen 5-tool schema, all complexity host-side, native Ollama `/api/chat` (no shim), local-first, small-model reliability, stdlib-heavy, macOS-first.* Anything that widens the model's world or bolts on a service is rejected unless it lives entirely host-side.

> **Reading guide.** Part I (atomic-agent) defines phases **P1–P12**. Part II (Loom) adds phases **P13–P19** and, where Loom independently confirms an atomic-agent finding, says so and **reinforces** the existing phase rather than duplicating it. The consolidated priority order is at the very end.

---

# Part I — atomic-agent → 2B

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

## Part I suggested order (P1–P12)

1. **P1 + P2** — tool-call robustness + never-throw closure. Highest thesis-fit, pure host-side, directly raises small-model reliability (the real accuracy lever, vs the grammar we can't take).
2. **P3** — parallel read batching. The biggest *speed* win; where atomic-agent's ~1.6× mostly comes from.
3. **P4** — loop-guard upgrade. Recovers stalls, caps runaway burn.
4. **P5 + P6** — prefix stability + context budgeting. Free latency (prefix cache) + long-context correctness.
5. **P7** — shell-command guard tiers. Safety floor + less prompt fatigue.
6. **P8, P9** — project instructions + eval rigor. Coding accuracy + a credibility edge over the surveyed benchmark.
7. **P10, P11** — drift replay + TUI polish.
8. **P12** — seatbelt sandbox, if we want to lead on safety.

**Honest footnote on the benchmark that prompted this.** The +11.3pp accuracy is within single-run noise (McNemar p≈0.3) and was measured with the opponent's skills disabled and its temperature uncontrolled; treat it as *not established*. The ~1.6× speed edge is more believable but partly reflects design differences, not pure loop efficiency. The right takeaway isn't "we're losing" — it's that the **loop mechanics above are real, portable, and on-thesis**, and that 2B can out-*rigor* the comparison by reporting CI-bounded numbers (P9).

---

# Part II — Loom → 2B

**What Loom is:** a Python local-ready *execution harness* that decomposes a goal into a subtask dependency graph, runs independent subtasks in parallel, verifies each with an independent model, and replans on failure — plus a fuzzy-matching edit tool, a verbatim SQLite conversation store with a `conversation_recall` tool, model routing (thinking/acting/verifier roles), and a Textual TUI. It shares 2B's core creed ("a weaker model in a strong harness beats a stronger model in a weak one") and, unlike atomic-agent, is **Python — so the good parts are liftable code, not just ideas.**

## The one-paragraph verdict

**Loom's whole-engine ambition is off-thesis, but its host-side reliability primitives are the best haul in either survey — and several drop straight into 2B's frozen schema.** The genuinely valuable, liftable wins are all *host-side and orthogonal to the 5-tool schema*: an **edit-ambiguity rejection layer** ("won't silently edit the wrong place"), **durable restart-surviving undo** covering create/delete/rename, **deterministic-first verification with a severity taxonomy**, a **path jail + command/exec hardening**, and **scoped-prompt + bounded-state discipline** that kills the "declares DONE after step 1 of 100" failure. Loom's two headline differentiators are weaker than advertised: its "**lossless memory**" is a compaction+archive+recall hybrid that **still compacts under budget**, and its recall path leans on a small model reliably driving a 9-action tool — the exact thing small models are worst at, so **lossless-recall does not beat 2B's existing compaction** and a `conversation_recall` 6th tool is not worth the frozen-schema cost. The heavyweight half (DAG fan-in, phase system, a 574-line iteration-gate DSL, multi-vote consensus, role/tier routing, a 200-line TOML with 40+ execution knobs) is exactly the complexity 2B's thesis is right to refuse — Loom's config surface is a useful *cautionary tale*. Net: 2B is already **ahead** on pre-approval inline diffs, mode cycling, headless one-shot mode, `--doctor`, install-time model grading, and `/undo`; take Loom's edit-safety, durable-undo, verification, and safety-hardening code, and decline its engine.

## A. What to TAKE from Loom (host-side, liftable Python) — ranked

| # | Take | Why it helps | Notes |
|---|---|---|---|
| L1 | **Edit-ambiguity rejection** — when a tolerant/fuzzy `edit_file` match is used, refuse if a *second* window also clears the similarity threshold and is within a small margin of the best (Loom: `second_ratio ≥ 0.85 and best−second < 0.05`). Plus a **uniqueness gate** (exact `old_text` appearing >1× → fail, don't replace-first) and a **corrective closest-match snippet** (numbered ±3 context lines) on failure. | "Won't silently edit the wrong place" — the one materially-better-than-tolerant-match property. The corrective snippet turns a dead-end into a retryable hint (disproportionately helps small models). | ~a few dozen lines of stdlib `difflib`. Builds on 2B's existing tolerant edits. **SKIP** tree-sitter narrowing (non-stdlib dep) and the batch `edits[]` mode (breaks the frozen single-`edit_file` contract). |
| L2 | **Durable undo** — a persistent, restart-surviving changelog + before-state snapshots covering **create / modify / delete / rename**, with per-scope revert (single / group / all). | 2B's undo is in-memory, edit-only, and lost on exit; this closes the biggest gap. | ADAPT + **improve on Loom**: Loom's writes and index-saves are non-atomic (torn-file / lost-history risk) despite its README — use temp+`os.replace`, and snapshot content on rename/dir-delete. Pure stdlib (`shutil`, `json`, `pathlib`). |
| L3 | **Deterministic-first verification + severity taxonomy** — run zero-model structural checks (file exists/non-empty, syntax parses, placeholder markers `[TODO]/[TBD]`, build/test exit code) *before* any model call; classify a failure as `infra`→retry, `semantic`/`hard`→replan, `advisory`→warn so the loop reacts correctly instead of binary pass/fail. | Cheapest reliability win; makes retry/replan decisions principled. Extends 2B's post-edit diagnostics. | Pure stdlib (`re`, `pathlib`, `subprocess`). Return a small verdict `{passed, severity, feedback}` where `feedback` flows into the next turn. |
| L4 | **Path jail** — a shared `Path(p).expanduser().resolve()` then `.relative_to(repo_root)` confinement for all 5 file tools, rejecting `../` traversal and outward symlinks with a clear error. | A safety layer 2B's stale-file detection + git guard don't cover. | ~15 lines stdlib. Skip Loom's multi-root read-map unless cross-repo reads are needed. |
| L5 | **Command/exec hardening** — bounded output reader (cap ~1 MB per stream, then drain-and-discard) + **kill-and-await subprocess on cancel/timeout** for `run_command`; a destructive-**git** blocklist (`push --force`, `reset --hard`, `clean -f`, `branch -D`, `checkout .`) with **no-shell argv exec**; and a **high-risk carve-out** that ignores a session "allow" grant and always re-prompts. | Prevents OOM from runaway output, orphaned processes, and silent destruction of uncommitted work that even undo can't recover. | *Reinforces & extends **P7*** (atomic-agent's hardline-block/safe-allow tiers). Merge into one command-safety phase. |
| L6 | **Scoped prompt + bounded regenerated state** — for any multi-step/subtask work, build a *fresh minimal prompt* (task + acceptance criteria + a compact, ring-buffered state object regenerated each call) instead of carrying full chat history. | Directly targets "declares DONE after step 1 of 100"; caps tokens flat regardless of task length; keeps small models oriented. | ADAPT. Most relevant if 2B grows subtask execution beyond `delegate`; the state-object habit (bounded decisions/errors, empties dropped) is useful even in the flat loop. |
| L7 | **Fresh-context self-check on high-stakes edits** — one optional gated pass where the *same* Ollama model reviews the diff against acceptance criteria from a **fresh prompt** (no self-justifying history), prompted as a skeptical reviewer. | Breaks a small model's self-consistency bias (rubber-stamping its own edit) for exactly one extra call — no second model needed, so it fits the single-model thesis. | ADAPT, gated to risky changes only. |
| L8 | **Memory hybrid (tool-free)** — keep 2B's lossy compaction, and add: (a) a **breadcrumb line** in the compaction summary ("N earlier messages summarized; full transcript in the session archive"); (b) make the existing sqlite store **searchable host-side** (indexed `tool_name`/`role` columns + a `LIKE` helper that *host code*, not a model tool, uses to auto-inject relevant archived turns on dangling references like "that file"/"earlier"); (c) a **tool-exchange-integrity invariant** in the compaction tail — never split an assistant-tool-call ↔ tool-result pair, and never lead the window with an orphan tool message. | ~80% of Loom's memory benefit at ~0 thesis cost, all host-side. The tool-exchange invariant fixes a real Ollama malformed-history crash risk. | ADAPT. **SKIP** the `conversation_recall` 6th tool, FTS5, the LLM/regex typed-memory indexer, and the semantic compactor. |
| L9 | **Anti-thrash fingerprinting** — hash `subtask_id/step + failure_reason` and refuse to re-attempt the same failing fix twice; pair with a couple of hard caps (iterations, wall-clock). | Small models loop hardest on the same broken step. | *Reinforces **P4*** (loop-guard). Fold the fingerprint idea into P4's veto ladder. |
| L10 | **Reproducible sampling** — pass an explicit low `temperature` **and a `seed`** in Ollama `options` for the main loop (and any verifier pass). | Loom sets neither; 2B can beat it on reproducibility, which strengthens P9's eval credibility. | *Reinforces **P1** (conservative sampling) + **P9** (eval).* Cheap. |
| L11 | **CLI/TUI power tools** — `/tool <name> key=val\|json` to invoke a frozen tool directly (trivial with only 5 tools; great for debugging); `/history search <q>` with next/prev jump over the append-only scrollback; risk-class labeling (write/execute/delete + impact text) on the inline confirmation. | Power-user ergonomics; the search fits 2B's append-only log naturally. | TAKE the three; small. |

## B. What NOT to take from Loom (and why)

| Not taking | Why |
|---|---|
| **`conversation_recall` as a 6th core tool** | Lossless-recall does **not** beat 2B's lossy compaction for a *small* model: it trades a guaranteed-in-window summary for a conditional fetch gated on the model noticing the gap and driving a 9-action tool correctly — the small model's weakest skills. Loom itself hedges (keyword nudges, archive-index menu) and *still compacts* in `/run`. If recall is ever added, expose **one** dumb `recall(query)` (stdlib `LIKE`), never the 9-action mega-tool. Prefer the host-side hybrid (L8). |
| **Decompose → DAG → parallel-subtask → replan engine** | The scheduler+async-gather core is small, but the surrounding output-conflict single-writer batching, fan-in finalizers, and ID-preserving replan contract are hundreds of lines solving problems a reactive coding agent doesn't have. Adopt only the *scoped-prompt/state* nuggets (L6); grow a plan object later only if a real need appears. |
| **Multi-vote consensus (Tier 3)** | N× model calls per decision — worst cost/benefit on latency-sensitive local models; disabled by default even in Loom; only meaningful with temperature variance (which 2B's reproducibility goal suppresses). |
| **Role/tier model router** | 2B is single-model native Ollama by design. A role/tier registry is complexity the thesis rejects. (Borrow only the graceful-degrade default: if an optional verifier is unavailable, skip-with-warning, don't block.) |
| **Iteration-gate DSL** (574 lines: `tool_metric`/`artifact_regex`/`command_exit`/`verifier_field` with operators) | A verification framework a small model can't reliably author specs for. Reuse only the one idea already in L3: run an allowlisted build/test command and gate on exit code. |
| **Process/phase system, `/learned`, `/telemetry`, `/auth`+`/mcp` manager tabs, tabbed multi-pane layout, animated activity glyph, the REST-API-server execution model** | Scope 2B has deliberately rejected (CLI/terminal-first, frozen schema). |
| **The `loom.toml` config surface (200 lines, 40+ execution knobs, three `[limits.*]` tables)** | The clearest anti-pattern to avoid. Nobody tunes `chars_per_token_estimate=2.8`; those belong in code constants. The one good idea is Loom's *runtime registry* (only **12** curated, typed, scoped entries exposed to live `/config`) — copy that shape **only if** 2B ever adds a live-config editor; never the file. |
| **Tree-sitter structural narrowing in the edit tool; batch `edits[]` mode** | Non-stdlib dependency / breaks the frozen single-`edit_file` contract, for marginal gain (the sliding-window path is the real engine). |

**Already ahead / at parity (no action):** pre-approval **inline line-numbered diff** (Loom's diff is post-execution; its approval modal shows only args); **shift+tab mode cycling** (Loom has no `/mode`); **headless `-p` one-shot stdout mode** and **`/undo`** (gaps Loom does *not* fill); **`--doctor`** (Loom's `loom doctor` is nice but 2B already has one) and **install-time model grading** (Loom discovers but doesn't grade); session list/`--resume`; per-session "allow" grants; command palette; streaming + tool one-liners.

## C. Roadmap additions (Loom) — phases P13–P19

Each is host-side, liftable stdlib Python, and independently shippable & unit-testable.

### Phase P13 — Edit-safety layer (L1)
- **Spec:** In `edit_file`'s tolerant path: (1) uniqueness gate — exact `old_text` matching >1× fails with a "add more context" message; (2) when falling back to fuzzy/whitespace-tolerant matching, compute the best and runner-up similarity and **refuse** if two windows both clear the threshold within a small margin (ambiguous); (3) on any miss, return a numbered closest-match snippet (±3 lines) so the model can retry. Add CRLF/mixed-line-ending tolerance.
- **Files:** `tools.py` (edit path), `orchestrator.py` (edit dispatch). New `tests/test_edit_ambiguity.py`.
- **Effort:** S–M. **Note:** highest-value Loom take; pure `difflib`.

### Phase P14 — Durable undo (L2)
- **Spec:** Replace/augment the in-memory pre-edit stack with a persistent changelog: a JSON index + before-state snapshots on disk (survives restart), covering create/modify/delete/rename, with revert-one / revert-group / revert-all. Atomic writes (`os.replace`) for both snapshots and index; snapshot content on rename and dir-delete.
- **Files:** new `changelog.py` (dep-free), wired into `edit_file`/`write_file` (and any delete/move). `persist.py` for the index location. New `tests/test_changelog_undo.py`.
- **Effort:** M.

### Phase P15 — Deterministic verification + severity (L3, L7)
- **Spec:** After a mutating turn, run zero-model checks (target file exists/non-empty, syntax parse for known types, placeholder-marker scan, optional allowlisted build/test exit code); classify failures by severity (`infra`/`semantic`/`hard`/`advisory`) and route (retry vs surface vs warn). Optionally, on high-stakes edits, one gated fresh-context self-check pass using the same model. Return a verdict carrying `feedback` into the next turn.
- **Files:** new `verify.py` (dep-free), wired in `orchestrator.py`. New `tests/test_verify.py`.
- **Effort:** M. Extends existing post-edit diagnostics.

### Phase P16 — Path jail + command/exec hardening (L4, L5) — *extends P7*
- **Spec:** (a) Shared canonicalize-and-confine helper for all 5 file tools. (b) `run_command`: bounded per-stream output reader with drain, kill-and-await on cancel/timeout. (c) `run_git`: destructive-pattern blocklist + argv exec (no shell). (d) High-risk carve-out that always re-prompts even under a session "allow". Build this *together with* P7's hardline-block/safe-allow tiers as one command-and-path-safety module.
- **Files:** `cmdguard.py` (merged with P7), `tools.py` (`do_run_command`/`_run_git`/path helper), `orchestrator.py`. New `tests/test_cmdguard.py` + `tests/test_path_jail.py`.
- **Effort:** M.

### Phase P17 — Memory archive hybrid (L8) — *complements P5/P6*
- **Spec:** Keep lossy compaction. Add: a breadcrumb line in the compaction summary; indexed `tool_name`/`role` columns + a host-side `LIKE` search helper; auto-inject the most relevant archived turn(s) when the latest user message contains dangling-reference phrases; enforce the tool-exchange-integrity invariant (never split call↔result; strip a leading orphan tool message) in the compaction tail. No new model-facing tool.
- **Files:** `persist.py` (schema/columns + search helper), `orchestrator.py` (compaction tail + injection). Extend `tests/test_persist.py`; new `tests/test_archive_inject.py`.
- **Effort:** M.

### Phase P18 — Scoped-prompt + bounded state (L6) — *optional, complements delegate*
- **Spec:** For `delegate`/sub-runner work (and optionally long single tasks), assemble a fresh minimal prompt with a compact, ring-buffered state object (bounded decisions/errors, empty fields dropped) regenerated each call, instead of carrying full history. Anti-thrash fingerprint (L9) folds into P4.
- **Files:** `subagents.py` (delegate prompt assembly), a small `state.py` dataclass. New `tests/test_scoped_state.py`.
- **Effort:** M. **Note:** only if 2B grows subtask execution; otherwise defer.

### Phase P19 — CLI/TUI power tools (L11)
- **Spec:** `/tool <name> key=val|json` direct invocation of a frozen tool (bypass the model); `/history search <q>` with next/prev jump over scrollback; risk-class label (write/execute/delete + one-line impact) on the inline confirmation.
- **Files:** `commands.py`, `app_tui.py`.
- **Effort:** S.

*(Reinforcements — no new phase: **L9** folds into **P4** (loop-guard fingerprint); **L10** folds into **P1** (sampling) + **P9** (eval seed/CI); **L5** merges into **P16**/**P7**.)*

---

## Consolidated priority order (P1–P19)

1. **P1 + P2** (tool-call robustness + never-throw closure) — real accuracy lever; +**L10** reproducible sampling.
2. **P13** (edit-safety layer) — highest-value Loom take, pure stdlib, guards the frozen `edit_file`.
3. **P3** (parallel read batching) — biggest speed win.
4. **P4** (loop-guard upgrade) — +**L9** anti-thrash fingerprint.
5. **P15** (deterministic verification + severity) — cheap, principled retry/replan; extends post-edit diagnostics.
6. **P14** (durable undo) — closes 2B's biggest undo gap.
7. **P16** (path jail + command/exec hardening) — absorbs **P7**; safety floor + less prompt fatigue.
8. **P5 + P6** (prefix stability + context budgeting) — free latency + long-context correctness.
9. **P17** (memory archive hybrid) — the right, tool-free answer to Loom's "lossless memory."
10. **P8, P9** (project instructions + CI-bounded eval) — coding accuracy + credibility edge.
11. **P19, P10, P11** (CLI/TUI power tools + drift replay + TUI polish).
12. **P18** (scoped-prompt/state) — only if subtask execution grows; **P12** (seatbelt sandbox) — if we want to lead on safety.

**Bottom line across both surveys.** atomic-agent proves the *speed* levers (parallel reads, stable prefix, early cutoff); Loom supplies the *reliability* primitives (edit-ambiguity rejection, durable undo, deterministic verification, safety hardening). Both validate 2B's frozen-schema/host-side thesis by showing the opposite: their differentiators that we *can't* or *shouldn't* take (GBNF grammar, KV quant, memory/intent fabrics, DAG engine, role routing, 200-line config) are exactly the complexity 2B is right to refuse. The take list is entirely host-side, mostly stdlib, and leaves the 5-tool schema untouched.
