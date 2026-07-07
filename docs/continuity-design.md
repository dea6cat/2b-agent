# 2B — conversation continuity (design)

**Status:** design only, no code yet. Host-side only; frozen 5-tool schema untouched.

## Problem

2B is task-per-message. A message typed while a turn is running is a *steer* (folded into
the running turn, and — since the steer re-attach change — carried into a continuation task
if it lands after the turn ends). But a message typed when **nothing is running** starts a
brand-new task with a **fresh conversation**: `enqueue_task` → `add_task` (`session.py:189`)
makes a `Task(conversation=None)`, and `run_task` builds a new `Conversation` from scratch
(`orchestrator.py:1363`). Proven empirically — two sequential top-level messages each see
only their own text; `t1.conversation is t2.conversation` is `False`.

This reset is **uniform across providers** — `is_local` only changes tool exposure and a few
nudges, never conversation carry. So cloud sessions are detached turn-to-turn too, for no
good reason: cloud models have 200k–1M windows and pay no penalty for keeping context.

## Decisions (settled)

1. **Cloud → continuous by default.** The per-message reset is an incidental limitation on
   cloud, not a deliberate choice. Cloud threads simply stay connected; there is no window
   pressure. No toggle needed to turn it *on*.
2. **Local → opt-in via `/continuity`.** On a small window (e.g. `qwen3.5:9b`, 13k) carrying
   every turn forward fills the context fast and forces constant compaction. So local stays
   detached by default; `/continuity` turns it on for the session, accepting that cost.
3. **Thread resets: `/clear` and `/new`.**
   - `/clear` — full reset (screen + tasks + thread), "like a new session" (existing).
   - `/new` — start a fresh thread but **keep the scrollback on screen**; the next message
     starts clean without wiping what you can see. The lighter, topic-switch reset.

The framing is: *fix an unnecessary reset on cloud, and give local users a knob* — not
"a local-only feature."

## Prerequisite (a real bug this exposes)

**The conversation never stores the model's final answer.** The DONE-finalize path
(`orchestrator.py:~1480`) sets `TaskState.DONE` and returns **without** `conv.append(msg)` —
only tool-call turns get appended (`orchestrator.py:1497`). Today that's invisible because
each task is thrown away. Under continuity it's a correctness bug: turn 2 would see turn 1's
tool calls and results but **not turn 1's synthesized answer**.

> This also silently degrades the **steer re-attach** already shipped — a re-attached
> continuation resumes without the prior final answer. Fixing it here repairs both.

**Fix:** persist the final assistant message onto the conversation before emitting
`TASK_DONE` (both the normal finalize and the max-turns final-answer path at
`orchestrator.py:~1605`). Guard against double-append. This is Phase 0 — independently
correct, worth landing even if continuity slips.

## Mechanism

Introduce a **session-level thread** the foreground line continues on:

- `Session.thread: Conversation | None = None` — the live conversation for the main line.
- When a new top-level task starts **and continuity is effective**, it adopts the thread:
  `task.conversation = session.thread` (or, if `None`, the task builds one and registers it
  back as `session.thread`). This reuses the exact adoption pattern already proven by resume
  (`app_tui.py:350`) and steer re-attach (`_flush_leftover_steer`).
- After a turn, `session.thread` *is* that (now-grown) conversation — nothing extra to do,
  since the task mutated the shared object.
- The thread tracks the **foreground line only**. Backgrounded tasks and `/tool` ephemeral
  carriers must **not** adopt or overwrite it (gate on `not task.ephemeral` and foreground).

Chose an explicit `Session.thread` pointer over "adopt `tasks[-1].conversation`" because
`tasks[-1]` can be a background or ephemeral task; the thread must follow the main line.

### Effective-continuity rule

A single tri-state override, so `/continuity` is symmetric across providers — the
provider only sets the **default**, and the user can always override it either way.

```
override = None            (auto — the default)
effective = override                     if override is not None
          = True                         if override is None and provider is cloud
          = False                        if override is None and provider is local
```

- `Session.continuity_override: bool | None = None`.
- The provider-derived default is evaluated against the **current** model at task-start
  (via `registry.resolve` + `registry.is_local`, `registry.py:54/60`), so switching models
  mid-session does the right thing while `override is None`. An explicit override sticks
  regardless of model.

## `/continuity` command

`@command("continuity")` (`commands.py` pattern; docstring's first line feeds the `/` menu).
Symmetric on both providers — it just sets the override. Surface is `on` / `off` only:

- **`/continuity on`** → `override = True`. On local, print a one-liner that the small
  window means it leans on compaction.
- **`/continuity off`** → `override = False`. This is the **cloud escape hatch**: it detaches
  an otherwise-always-continuous cloud session so the next message starts fresh. On local
  it's the (already-default) detached state, made explicit.
- **Bare `/continuity`** → toggles the current effective state (flip on↔off) and prints the
  result. Convenient for a two-state control.

Internally the flag stays tri-state (`None` = untouched → follow the provider default), so a
session starts continuous on cloud / detached on local with no command, and switching models
before any explicit toggle still tracks the provider. There is no command that returns to
`None`; once you've chosen `on`/`off` it sticks for the session — which is the intent.

Status/indicator: `⛓ thread` in the status bar **only when a thread is live** (i.e. effective
continuity is on *and* a thread exists), mirroring the mode indicator (`MODE_LABELS` / status
render). Absent otherwise — so on local, seeing `⛓ thread` confirms your messages are
connected; on cloud, its absence after `/continuity off` confirms you've detached.

## Reset semantics

- **`/new`** (`@command("new")`): `session.thread = None`, `session.active_task_id = None`,
  keep `session.tasks` and the on-screen log. Next message begins a fresh thread. Refuse
  while a task is ACTIVE (same guard as `/clear`, `commands.py:744`).
- **`/clear`** (existing, `commands.py:741`): additionally clear `session.thread = None`.
  (Already clears tasks + screen; the thread pointer just needs to be dropped too.)

## Compaction interaction

No new machinery. `_maybe_compact` already runs each turn (`orchestrator.py:1397`, trigger
`COMPACT_AT = 0.75`) over the task's conversation. A continued thread compacts automatically
when it crosses 75% of the window; on local this fires often — that is the accepted cost of
opting in, and the ctx meter (already amber past 80%) makes it visible. The searchable
compaction archive + P17 recall (`_maybe_inject_recall`) already let elided turns be pulled
back, so continuity composes with them for free.

## Edge cases

- **Model switch mid-thread (cloud → local):** the thread persists; effective continuity
  re-evaluates to the local rule. A large cloud-built thread may overflow a small local
  window on the next turn — compaction absorbs it, but note the latency spike. Acceptable.
- **Local → cloud:** thread persists and simply continues; no risk.
- **Steer re-attach convergence:** with continuity on, the normal path already carries
  context, so leftover-steer re-attach becomes a special case of the same mechanism. Keep
  `_flush_leftover_steer` for the continuity-off case (it must still carry the *immediate*
  steer); ensure the two don't double-adopt the same conversation.
- **Resume-from-disk** (`_resume_conv`): adopt the resumed conversation as the initial
  `session.thread` so a resumed session is continuous from message one (on cloud) / on opt-in
  (on local).
- **`/tool` ephemeral + backgrounded tasks:** excluded from the thread (see Mechanism).

## Phasing

- **Phase 0 — persist final answers** (prerequisite bug fix; independently valuable).
- **Phase 1 — cloud continuity** (session thread + adoption; cloud effective=True; `/new`;
  `/clear` drops thread). Ship + validate.
- **Phase 2 — local opt-in** (`Session.continuity_on`, `/continuity` command, indicator,
  compaction-pressure messaging).

Each phase lands as a single reviewed feature commit, host-side, schema frozen — consistent
with the shipped-work discipline in `roadmap-handoff.md`.

## Testing

- **Phase 0:** unit — a DONE turn appends its final assistant message; a re-run/continuation
  sees the prior answer. Extend `test_turn_closure.py`.
- **Phase 1:** integration (fake provider, like `test_steer.py`) — two sequential top-level
  tasks on a cloud provider share one conversation and turn 2's request includes turn 1's
  exchange; `/new` breaks the chain; `/clear` breaks it and wipes.
- **Phase 2:** local provider stays detached by default; `/continuity` connects it;
  `/continuity off` re-detaches. Detector/flag unit tests + a Pilot test for the command and
  indicator.

## Resolved

1. **Indicator** — status bar, `⛓ thread`, shown **only when a thread is live**.
2. **Cloud escape hatch** — `/continuity off` works on cloud (and local), via the tri-state
   override above. Cloud is continuous by default but explicitly detachable; `/new` remains
   the way to start a fresh thread without changing the default.
3. **Command surface** — `on` / `off` only (plus a bare-toggle); no `auto` verb.
4. **Persistence across restarts** — none. Continuity is strictly an in-session property. A
   restart is an intentional history reset (you restart *because* you want to start clean),
   so there is no cross-restart thread to reload beyond what explicit resume already offers.

All open questions resolved — design is ready to implement (Phase 0 first).
