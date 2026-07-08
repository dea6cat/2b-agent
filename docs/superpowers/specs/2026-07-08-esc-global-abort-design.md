# ESC — immediate global abort

**Date:** 2026-07-08
**Status:** Approved, pending implementation

## Problem

Pressing ESC does not stop 2B immediately. It sets `task.cancel_flag`, but that flag
is only *observed* at Python-level checkpoints: orchestrator loop boundaries (between
turns and tool calls) and per-chunk inside `on_text`. When a worker thread is parked
inside a blocking `urllib` socket operation, nothing looks at the flag, so ESC has no
effect until the block ends:

- **Anthropic provider** uses a non-streaming fallback (`stream()` → `send()` →
  `post_json`). It fetches the entire model response in one blocking HTTP call, then
  calls `on_text` once at the end. ESC waits out the whole response.
- **All providers** block inside `urlopen` during TCP connect, and inside
  `for raw in resp` in the gap between tokens. The flag is never checked there.
- **Compaction** (`_maybe_compact` → `provider.stream` at `orchestrator.py:676`) calls
  the provider with no `cancel` and a callback that never checks the flag — also
  uninterruptible.
- **Backgrounded tasks** (Ctrl-B) are not targeted at all. `action_interrupt` only
  touches `session.active_task`, so a backgrounded task keeps generating after ESC.

Setting a flag cannot wake a thread blocked in C. To be truly immediate, ESC must close
the socket out from under the blocked thread, forcing the read to raise and unwind.

## Goal

A single ESC press is a **global panic button**: it immediately aborts every task's model
call (thinking/generating), every running command/subprocess, and tears down helpers —
for both cloud and local models, in any state (connecting, waiting, mid-stream, mid-tool).
Scope confirmed with the user: **everything, all tasks** — foreground *and* backgrounded.

## Approaches considered

- **A. Abortable connections — close the socket on ESC (chosen).** A registry of live HTTP
  responses; ESC closes them all so any parked network thread raises immediately. Combined
  with setting every task's flag and the existing ~100 ms subprocess kill. Truly immediate;
  truly aborts (connection dies, no zombie request); identical for cloud and local.
- **B. Orphan the workers.** Mark tasks stopped in the UI and stop waiting; let orphaned
  HTTP calls finish and discard results. Feels instant but violates "everything is aborted"
  — the request keeps running server-side / the local model keeps generating, threads
  linger. Rejected.
- **C. Short socket timeouts + poll.** Loop with ~100 ms socket read timeouts, checking the
  flag between reads. Adds latency, wastes cycles, doesn't cleanly cover the connect phase.
  Rejected.

## Design (Approach A)

### 1. Abortable HTTP layer (`providers/base.py`)

- Module-level registry of live response objects: a `set` guarded by a `threading.Lock`.
- `post_stream` and `post_json` gain a `cancel: threading.Event | None` parameter:
  - Check-then-raise a `_Cancelled` sentinel exception if `cancel` is already set (before
    opening the connection — don't even start).
  - Register the `resp` object in the registry immediately after `urlopen` succeeds;
    deregister in a `finally`.
  - For `post_stream`, check `cancel` on each line before yielding.
- New `abort_all_connections()`: closes every response currently in the registry
  (`resp.close()`). A close mid-read raises `OSError`/`ValueError` in the blocked thread.
- Error translation: when the flag is set, any `OSError`/`ValueError`/`URLError` raised by a
  closed socket is re-raised as `_Cancelled` (non-retryable), so it unwinds to
  `_finish_stopped` and never surfaces to the user as a provider error.

**Known bounded window:** if ESC lands *during* TCP connect (before `resp` is registered),
there is no socket object to close yet; that thread aborts when connect completes or its
connect-timeout fires. Usually sub-second. Documented rather than solved with heavier
machinery. A connect-timeout floor may be added if it proves noticeable in practice.

### 2. Anthropic → real streaming (`providers/anthropic.py`)

- Rewrite `stream()` to consume the Messages API SSE stream via `post_stream` instead of the
  blocking `send()`:
  - `content_block_start` — begin a text or `tool_use` block.
  - `content_block_delta` — `text_delta` → `on_text(chunk)`; `input_json_delta` →
    accumulate tool-call argument JSON.
  - `message_delta` / `message_stop` — stop reason and usage (prompt/output tokens).
- Thread `cancel` into `post_stream`.
- Preserve the existing prompt-cache `cache_control` headers already built in `send()`.
- `send()` is kept for any non-streaming caller. Bonus: Anthropic now streams tokens live
  instead of dumping the whole response at once.

### 3. Thread `cancel` through every provider call

- `stream_with_retry` forwards its `cancel` argument into `provider.stream(...)`.
- All four providers' `stream()` signatures accept `cancel` and pass it to
  `post_stream` / `post_json`.
- Compaction: `_maybe_compact` (and the `provider.stream` call in the summarizer) passes the
  task's `cancel_flag` so ESC aborts an in-progress compaction too.

### 4. Centralized `abort_all()` panic routine

- Iterate **all** tasks in `session.tasks`; for each in state `ACTIVE` or `BACKGROUNDED`:
  `clear_steer()` then `cancel_flag.set()`.
- Call `abort_all_connections()` to unblock every parked network thread instantly.
- Subprocess tools die within ~100 ms via their existing `cancel` poll (flag now set);
  `teardown_helpers()` runs off the UI thread (existing behavior).
- `action_interrupt` (`app_tui.py`) becomes this panic, keeping the existing
  history-search-escape special case (ESC closes an open scrollback search first). It
  announces "stopping everything…", snaps scrollback to the bottom, and marks tasks stopped.

## Data flow on ESC

```
ESC → action_interrupt → abort_all()
  1. set cancel_flag on every active/backgrounded task   (instant)
  2. abort_all_connections() closes every live socket    (instant unblock of net threads)
  3. worker threads raise _Cancelled / _Interrupted → _finish_stopped (per task)
  4. subprocess tools see the flag on next ~100 ms poll → killpg their process group
  5. teardown_helpers() tears down LSP/MCP off-thread
  6. UI announces, snaps scrollback, marks tasks stopped
```

## Error handling

- `_Cancelled` is a dedicated sentinel, non-retryable. `stream_with_retry` re-raises it
  immediately (never retries a cancelled call).
- The orchestrator maps cancellation to `_finish_stopped`, not `_finish_failed` — a
  user-initiated abort is not an error and must not be surfaced as one.
- Closing an already-closed or never-registered response is a no-op / swallowed.

## Testing

- `abort_all_connections()` closes a registered fake response object.
- `post_stream` with a pre-set `cancel` raises `_Cancelled` without connecting.
- `post_stream` raises `_Cancelled` (not `ProviderError`) when its response is closed
  mid-iteration and `cancel` is set.
- Anthropic SSE parser turns a canned event stream into the correct `ProviderResponse`
  (text + `tool_use` + usage) and honors `cancel` between events.
- `stream_with_retry` does not retry when cancelled mid-stream.
- Orchestrator-level: a fake provider whose `stream` blocks on an `Event`; `abort_all()`
  sets flags + closes the fake connection → both an active and a backgrounded task reach
  `_finish_stopped`.
- Existing subprocess-cancel behavior still passes (flag set → `killpg` within ~100 ms).

## Out of scope (YAGNI)

- A separate subprocess registry for immediate `killpg` — the existing 100 ms poll already
  meets the perceptual "immediate" bar.
- Changes to the double-Ctrl-C quit semantics.
- Solving the TCP-connect window with anything beyond an optional connect-timeout floor.
