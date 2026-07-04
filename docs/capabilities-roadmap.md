# Crush → 2B: what to take, what not, and a roadmap

**Date:** 2026-07-03
**Source analyzed:** `/Users/do519-lap/repo_apps/crush` (Charmbracelet Crush — Go, ~973 files, cloud-first, SQLite-backed, client/server, LSP+MCP+skills+hooks).
**Filter applied:** 2B's thesis — *frozen 5-tool schema, all complexity host-side, native protocols (no shim), local-first, small-model reliability.* Anything that widens the model's world is rejected unless it can live entirely host-side.

---

## The one-paragraph verdict

Most of Crush's *surface* is the opposite of 2B: it hands the model ~25 tools (download, fetch, sourcegraph, web_search, job_output/kill, crush_info, lsp_diagnostics/references/restart, multiedit, todos, agentic_fetch…), runs a headless HTTP daemon, and leans on Go-only frameworks (`fantasy`, `catwalk`). None of that transfers — and Crush actually *validates* a 2B design choice: 2B folds LSP diagnostics into the edit result host-side, while Crush exposes them as model-facing tools; 2B's approach is strictly better for small models. **The gold is entirely in Crush's host-side mechanisms:** session persistence, edit-safety (stale-file detection), loop detection, richer edit-error recovery, permission granularity, and a lot of TUI polish. Several of these directly fix the failure mode this session's eval surfaced (the D3 "old_text not found" retry-until-timeout loop).

---

## A. What to TAKE (host-side, fits the thesis)

| # | From Crush | Why it fits 2B | Value | Effort |
|---|---|---|---|---|
| T1 | **Loop detection** (`loop_detection.go`: hash tool-call+result over a window, stop if repeated) | Pure host-side; model-agnostic. Directly fixes the D3 loop we found today. | ★★★ | S |
| T2 | **Recoverable edit errors** (Crush's edit errors are explicit; 2B's model re-submits the same near-miss) | Host-side error text only; helps small models self-correct instead of looping | ★★★ | S |
| T3 | **Stale-file detection** (read-before-write + mtime > last-read → reject, tell model to re-read; `edit.go`/`filetracker`) | Safety; prevents clobbering the user's concurrent edits. Extends 2B's "catches its own mistakes." | ★★★ | S–M |
| T4 | **Session persistence** (SQLite: sessions + messages + file versions per project; resume, summary-checkpoint replay) | Host-side; Python's stdlib `sqlite3` = zero new deps; local-first friendly | ★★★ | M |
| T5 | **File version history → multi-level `/undo`** (`internal/history`: pre/post content snapshots per edit) | Host-side; 2B has only single-step undo today | ★★ | M (rides T4) |
| T6 | **Context-window meter + ≥80% warning** (sidebar `~%`, warn icon) | 2B's whole pain is tiny local windows — a live meter is high-signal | ★★★ | S |
| T7 | **Richer diff review** (syntax-highlighted unified/split diff in the confirm dialog; `diffview`) | Improves 2B's existing confirmation UX; Textual can render it | ★★ | M |
| T8 | **Collapsible tool-call detail** (status icon + spinner, expand/collapse; `chat/tools.go`) | Polishes 2B's narrated tool tree without changing the model's world | ★★ | M |
| T9 | **@-file completion + command palette** (`completions/`, `dialog/commands.go`, Ctrl+P) | Discoverability + fewer path-typos; host-side UX | ★★ | M |
| T10 | **Task-finish notification** (OSC 9/777 + bell; `notification/`) | Local runs are slow; ping when a backgrounded task finishes | ★★ | S |
| T11 | **Per-session "always allow" + allowlist** (`permission.go`: `tool:action`, per-dir grants, auto-approve-session) | Refines 2B's normal/accept-edits/plan modes with finer, remembered grants | ★★ | M |
| T12 | **Model context-window catalog** (`catwalk`: per-model ContextWindow/caps) | Makes 2B's auto-compaction + read-caps accurate for cloud models (today: per-provider constants) | ★★ | S–M |
| T13 | **Control-char escaping of raw tool output** (`ansiext.Escape` → Control Pictures) | Small robustness — shell output can't corrupt the TUI | ★ | S |
| T14 | **PreToolUse hooks** (`internal/hooks`: shell hook can block/allow/transform/inject) | Host-side extension point; power-user, opt-in | ★ | M |
| T15 | **Skills (agentskills.io) — CLOUD/opt-in only** (`SKILL.md` frontmatter, injected as `<available_skills>`, loaded via read) | Conditionally useful for cloud; **risky for local** (context bloat/distraction) | ★ (cloud) | M |

Effort: S = <½ day, M = 1–2 days, ★ scale = value to 2B.

---

## B. What NOT to take (and why)

| From Crush | Why it's rejected for 2B |
|---|---|
| **~20 extra model-facing tools** (download, fetch, sourcegraph, web_search/web_fetch, job_output/job_kill, crush_info, crush_logs, multiedit, todos, list/read_mcp_resource) | Directly violates the frozen-5-tool thesis. Every added tool erodes small-model reliability — the exact failure 2B was built to avoid. |
| **`lsp_diagnostics` / `lsp_references` / `lsp_restart` as *tools*** | 2B already does diagnostics **host-side, folded into the edit result** — strictly better for small models. Crush's tool-based approach is the anti-pattern here. Keep 2B's. |
| **Client/server daemon** (headless HTTP/JSON + h2c + SSE, multi-client attach; `server/`,`client/`,`proto/`,`backend/`) | Heavy architecture for multi-client/detached runs. 2B is single-user local-first; this is complexity with no matching need. (Far-future only if 2B ever wants headless/remote.) |
| **Embedded POSIX shell** (`mvdan.cc/sh`) | Exists for Windows portability. 2B is macOS-first and just shipped real process-group subprocess control (esc-kill). Not needed. |
| **Background *shell jobs*** (50 concurrent, 8h retention, job_* tools) | 2B backgrounds at the *task* level (Ctrl+B) — cleaner and not model-facing. Shell-job tools add model-facing surface. |
| **OAuth device flows** (Copilot, Hyper) | Provider-specific; 2B is local-Ollama + API-key. Only worth it if 2B ever targets Copilot. |
| **Go agent framework** (`charm.land/fantasy`) + **remote catalog service** (`catwalk` HTTP/ETag sync) | 2B has its own native per-provider adapters (a core differentiator — no shim). Adopt the *catalog data concept* (T12), not the framework/service. |
| **Cross-platform matrix** (Windows/Android/BSD) | Out of scope by design (macOS-first; Linux already deferred). |
| **Skills for LOCAL models** | Injecting a skills catalog + usage instructions bloats a small window and distracts small models — against the thesis. Cloud-only/opt-in at most (T15). |

---

## C. Roadmap (phased, with specs + implementation plans)

> **Phase 1 (loop detection + recoverable edit errors) is shipped** — its spec has been removed from this roadmap. See `orchestrator._LoopGuard` and `tools._nearest_hint` (tests: `tests/test_loop_guard.py`, `tests/test_edit_file.py`).

> **Phase 2 (stale-file / read-before-write detection) is shipped** — its spec has been removed from this roadmap. 2B records each file's mtime when it reads it (`orchestrator._record_read`, keyed via `tools.resolve_read_path`) and refuses an edit/write to a file that changed on disk since (`_stale_check`), refreshing after its own writes (`_refresh_mtime`); the guard also covers worker/`delegate` batch writes. Tests: `tests/test_stale_edit.py`, `tests/test_worker_apply.py`.

### Phase 3 — Session persistence (local-first, stdlib `sqlite3`)

**Phase 3 is shipped in full** — specs removed.

> **3.1 (persistence) + 3.2 (resume/list):** `persist.py` saves each task's conversation to `~/.config/2b/history.db` (stdlib sqlite3, zero deps; `TWOB_NO_HISTORY` to disable) at task end, keyed by `(task id, cwd)`. `conversation.to_jsonable/from_jsonable` round-trip the thread. `2b --continue` / `--resume <id>` / `--list-sessions` and `/sessions`; the resumed thread (and its id) attaches to the first task so the next message continues it and updates the same row. Tests: `tests/test_persist.py`.

> **3.3 (multi-level `/undo`):** each edit/write pushes its pre-content onto a per-task stack (`Task.edit_history`, `Task.push_edit`, capped at 50); `/undo` reverts the last edit, `/undo N` the last N, `/undo <path>` the most recent edit to that file (new files are removed). In-memory rather than a persistent `file_versions` table — a deliberate simplicity call, since undo is a within-session action. Tests: `tests/test_undo.py`.

### Phase 4 — TUI enhancements (the "improve 2B's TUI" ask)

2B's TUI is Textual (Python) with streaming, a plan checklist, narrated tool actions, themes, and a status line. Crush's TUI is Bubble Tea v2. Port the *ideas*, not the code.

> **4.1 (context-window meter) is shipped** — the TUI status bar shows live `ctx N%` of the model's window, amber at ≥80%. `orchestrator.context_usage` (pure, tested); budget resolved off the render path (`_load_ctx_label`, refreshed on `/model`·`/default` via `on_model_changed`); the per-render estimate is memoized so it recomputes only when a message is added. Tests: `tests/test_context_meter.py`.

> **4.2 (diff review) is shipped — and went further: inline, no modal.** Write/edit confirmations render in the conversation view (per user direction, Claude-Code style): a colorized, line-numbered unified diff (added on green bg, removed on red, context dim) then an inline `apply? y / n / esc` answered by a keypress. The `ConfirmScreen` modal was removed. Diff parsing is pure in `difffmt` (tested). Also fixed alongside: shell-chained `run_git` is rejected before prompting, and tool-error sub-lines show up to 400 chars. Tests: `tests/test_render_diff.py`, `tests/test_run_git_shell.py`.

> **4.4 (@-file completion) is shipped.** Typing `@partial` offers matching project files in the existing inline suggestions strip; Tab/Enter inserts the path. Pure helpers in `completion` (`at_token`, `rank_files`), tested. The command palette already existed inline. Tests: `tests/test_completion.py`.

> **4.5 (finish notification) is shipped.** A task finishing while the terminal is unfocused posts an OSC 9 desktop notification (written to /dev/tty, so Textual's screen is untouched); off with `TWOB_NO_NOTIFY`. Pure builder in `notify.osc9`, tested. Tests: `tests/test_notify.py`.

> **4.3 (tool-line detail) is shipped as A + B** (chosen from a visual mockup). **A — live spinner:** the tool line mounts on start with an animated spinner + elapsed and is finalized in place to ✓/✗ (no re-append). **B — one-line detail:** the line carries a compact result summary — edits show `+N −M` (from the diff), reads `N lines`, writes `N bytes`, errors `exit N`; the full error message still gets a sub-line. An interrupted tool settles to a `·` "stopped" line. True fold/unfold collapse was deliberately **not** built — the transcript is append-only, and folding would need a render rewrite the phase doesn't warrant. Pure summary logic in `toolline`, tested. Tests: `tests/test_toolline.py`.

> **4.6 (session switcher) is shipped — inline, not a modal** (per the no-popup preference). `/sessions` (and `2b --list-sessions`) list saved sessions newest-first with id · relative age · model · message count, plus a resume hint (`2b --resume <id>` / `2b --continue`). `persist.list_sessions` now returns the message count and `persist.relative_age` formats the age (tested). In-TUI live resume isn't offered — 2B's per-task-conversation model makes resume a launch-time action (`--continue`/`--resume`), which is what the hint points to.

**Phase 4 is shipped in full** (4.1–4.6). The TUI now has: a live context-window meter, inline line-numbered diff confirmation, live tool spinners + one-line detail, @-file completion, unfocused finish notifications, and an enriched session list.

### Phase 5 — Control refinements (optional / power-user)

> **5.1 (per-session allow) is shipped.** The inline confirm now offers a third key — `a` "allow all edits/writes/git/commands this session" — alongside `y`/`n`. `Session.granted` holds the tool keys allowed this session; `request_confirmation(grant_key=…)` auto-approves a granted tool without prompting; a config `allowed_tools` list pre-grants at startup. Per-tool granularity (not the coarse accept-edits mode), so you can trust `run_command` while still reviewing edits. Tests: `tests/test_grants.py`.

> **5.2 (PreToolUse hooks) was skipped by design.** A configurable shell-command-before-every-tool extension point is a team/policy feature; for a personal, local-first agent it adds config surface and a shell path for a thin audience, and the safety needs it would serve are already covered by plan mode, per-command confirmation, 5.1 grants, and the `run_git` git-only guard. Phase 5 is complete with 5.1.

### Phase 6 — Model catalog (optional)

> **6.1 (per-model catalog) is shipped.** A bundled `catalog.json` (model → context_window, default_max_tokens, supports_images) with a longest-prefix `catalog.py` loader gives cloud models their real context window for auto-compaction and read-caps, instead of a coarse per-provider constant. `orchestrator.context_budget` consults it (Ollama, local and cloud, still sizes its own window dynamically and is absent from the catalog); `providers/anthropic.py` uses `catalog.max_tokens` for its output cap; `--print-ctx` surfaces window/output/image support for catalogued models (ollama-first so a locally-pulled name collision reports real `num_ctx`). A missing/corrupt catalog degrades to "unknown" and never crashes startup. Tests: `tests/test_catalog.py`. (Landed as `catalog.py`, not `registry.py` — that name was already the provider registry.)

---

## Suggested order

1. ~~Phase 1 (loop detection + recoverable edit errors)~~ — **shipped.**
2. ~~Phase 2 (stale-file detection)~~ — **shipped.**
3. **Phase 4.1** — one-afternoon TUI win (context meter) that serves the local-window thesis.
4. ~~Phase 3 (persistence + resume/list + multi-level undo)~~ — **shipped**; stdlib-only.
5. ~~Phase 4 (context meter, inline diff, tool spinners+detail, @-completion, notifications, session list)~~ — **shipped.**
6. ~~Phase 5 (per-session allow grants)~~ — **shipped** (5.1); 5.2 hooks skipped by design.
7. ~~Phase 6 (per-model context-window/capability catalog)~~ — **shipped.**

Each phase item is independently shippable on its own branch and testable without a live model (the reliability + safety + persistence items are all unit-testable host-side).
