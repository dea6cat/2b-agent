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

2B's biggest missing capability vs Crush: sessions/tasks are **in-memory only** — nothing survives a restart. Python ships `sqlite3`, so this adds **zero dependencies**.

#### 3.1 Persistence layer  (T4)
- **Spec:** A `persist.py` storing, per project, sessions and their messages so a session can be listed and resumed. Store DB at `~/.config/2b/history/<project-hash>.db` (or `<project>/.2b/history.db`). Tables: `sessions(id, title, created_at, updated_at, model, cwd)`, `messages(id, session_id, role, parts_json, created_at)`. Persist the canonical `Conversation` (roles + text + tool calls/results as JSON parts — mirror Crush's single `parts` JSON column). Write incrementally as turns complete (debounce not needed at 2B's scale — flush per turn).
- **Files:** new `src/two_b/persist.py`; hook into `orchestrator.run_task` (append messages as `conv` grows) and `session.py` (Session gets a persistent id + db handle).
- **Approach:** stdlib `sqlite3`, WAL mode, a tiny DAO (no ORM). Serialize `Message` via a `to_dict`/`from_dict` on `conversation.py`. Keep it lazy — persistence is opt-out-able (`TWOB_NO_HISTORY`).
- **Tests:** round-trip a Conversation through save/load; list/resume.
- **Effort:** M.

#### 3.2 Resume / list UX  (T4)
- **Spec:** `2b --continue` (resume most recent session in this project), `2b --resume <id>`, and `/sessions` (list + pick) in the TUI. On resume, rebuild the `Conversation` from stored parts; if the session was auto-compacted, replay from the summary checkpoint (store `summary_message_id` like Crush).
- **Files:** `cli.py` (flags), `commands.py` (`/sessions`), `app_tui.py` (session picker — see T… TUI phase), `persist.py` (queries).
- **Effort:** M (UI piece rides Phase 4).

#### 3.3 Edit history → multi-level `/undo`  (T5)
- **Spec:** On each successful edit/write, snapshot pre-content into a `file_versions(session_id, path, version, content, created_at)` table (Crush stores full content, not diffs — simple + robust). `/undo` reverts the last N edits; `/undo <path>` reverts a specific file. Also snapshot when the on-disk content differs from the last recorded version (captures external edits — ties to 2.1).
- **Files:** `persist.py`, `orchestrator.apply_edit/apply_write` (snapshot hook), `commands.py` (`/undo`).
- **Effort:** M (rides 3.1).

### Phase 4 — TUI enhancements (the "improve 2B's TUI" ask)

2B's TUI is Textual (Python) with streaming, a plan checklist, narrated tool actions, themes, and a status line. Crush's TUI is Bubble Tea v2. Port the *ideas*, not the code.

#### 4.1 Context-window meter + ≥80% warning  (T6) — **highest TUI ROI**
- **Spec:** Show live `context: 63% (5.0k/8k)` in the status line/sidebar, turning amber ≥80%. 2B already estimates tokens for auto-compaction (`CONTEXT_BUDGETS`, `context_budget`) — surface it. Critical because small local windows are 2B's core constraint.
- **Files:** `app_tui.py` (status render), `tui.py` (`render_session`), reuse `orchestrator.context_budget` + the running token estimate.
- **Effort:** S.

#### 4.2 Richer diff review  (T7)
- **Spec:** In the edit confirmation, render a syntax-highlighted unified diff (Textual supports Rich `Syntax`/panels); optional split view on wide terminals; truncate with "N more lines" + expand.
- **Files:** `app_tui.py` (`_prompt_confirmation` / the pending-confirmation render), a `diffview` helper. 2B already computes `task.last_diff`.
- **Effort:** M.

#### 4.3 Collapsible tool-call detail  (T8)
- **Spec:** Each narrated tool line gets a status glyph (⏳/✓/✗) + spinner while running; expandable to show full args/result. Keeps the clean tree by default.
- **Files:** `app_tui.py`, `tui.py`.
- **Effort:** M.

#### 4.4 @-file completion + Ctrl+P palette  (T9)
- **Spec:** Typing `@` opens a fuzzy file-path completion (multi-tier rank: exact/prefix/segment); Ctrl+P opens a fuzzy command palette over 2B's slash commands. Reduces path typos (which cause edit-not-found loops — ties to Phase 1).
- **Files:** `app_tui.py` (input handling), a completions widget; fuzzy over `repomap`/`list_files`.
- **Effort:** M.

#### 4.5 Task-finish notification  (T10)
- **Spec:** When a backgrounded task finishes, emit an OSC 9/777 desktop notification (fallback bell), suppressed when focused. Long local runs need this.
- **Files:** `app_tui.py`; a small `notify.py` (OSC sequences; detect SSH/terminal).
- **Effort:** S.

#### 4.6 Session switcher UI  (rides T4)
- **Spec:** `/sessions` opens a filterable list (title, age, model, message count) with resume/rename/delete; a two-step delete confirm.
- **Files:** `app_tui.py` (modal), `persist.py`.
- **Effort:** M.

### Phase 5 — Control refinements (optional / power-user)

#### 5.1 Per-session "always allow" + allowlist  (T11)
- **Spec:** In normal mode, a confirm dialog offers "allow once / allow for session / deny." "Allow for session" remembers `(tool, action, dir)` grants in-memory (like Crush's `sessionPermissions`); a config `allowed_tools` list pre-grants. YOLO already ≈ `--yes`.
- **Files:** `session.py` (grant store), `orchestrator.request_confirmation`, `app_tui.py` (3-way dialog), `config.py`.
- **Effort:** M.

#### 5.2 PreToolUse hooks  (T14)
- **Spec:** Optional `hooks` config: shell commands run before a tool call that can block (exit 2), allow, or inject context (stdout). Host-side; off by default.
- **Files:** new `hooks.py`, `orchestrator._dispatch_tool`, `config.py`.
- **Effort:** M. **Note:** power-user; low priority.

### Phase 6 — Model catalog (optional)

#### 6.1 Per-model context-window/capability catalog  (T12)
- **Spec:** A small bundled JSON (model → context_window, supports_images, default_max_tokens) so auto-compaction/read-caps are accurate per cloud model instead of per-provider constants. Ollama stays dynamic (`num_ctx`).
- **Files:** `catalog.json` + `registry.py`/`orchestrator.context_budget`.
- **Effort:** S–M.

---

## Suggested order

1. ~~Phase 1 (loop detection + recoverable edit errors)~~ — **shipped.**
2. ~~Phase 2 (stale-file detection)~~ — **shipped.**
3. **Phase 4.1** — one-afternoon TUI win (context meter) that serves the local-window thesis.
4. **Phase 3 (3.1 → 3.2 → 3.3)** — the big capability (persistence/resume/undo); stdlib-only.
5. **Phase 4.2–4.6** — TUI polish, incrementally.
6. **Phase 5 / 6** — optional, as needed.

Each phase item is independently shippable on its own branch and testable without a live model (the reliability + safety + persistence items are all unit-testable host-side).
