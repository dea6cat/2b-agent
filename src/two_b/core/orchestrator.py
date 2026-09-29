"""The agentic turn loop, threaded and event-emitting (Milestone 2).

Faithful port of the prototype's loop — same Ollama native /api/chat call, same
message list, same tool dispatch, same MAX_TURNS, same content->thinking
fallback. Structural changes for M2, all host-side (the model's world is
unchanged):

  - A task runs on its own worker thread. The worker NEVER writes to the
    terminal: every print() the verbatim do_* tools emit is captured via
    redirect_stdout and shipped to the UI thread as an event. The UI thread is
    the sole owner of stdout / rich.Live / input(). This is what lets the tools
    stay byte-for-byte unchanged while running off the main thread.
  - Write/edit confirmations are routed to the UI thread through the task's
    PendingConfirmation (see request_confirmation). A backgrounded task blocks
    there until it is foregrounded — the "pause on write" behavior.
  - Plan steps are parsed from the model's own first-turn text and their
    active/done state inferred from tool calls (planparse), purely for display.

Milestone 3: the model I/O now goes through a provider adapter (resolved from
the registry by the active model), driven by the canonical Conversation. The
loop, confirmation routing, plan parsing, and events are unchanged — only the
transport is abstracted. Local Ollama still reaches its native /api/chat.

The loop's helpers live beside it: taskrun (per-run state), answer (a turn with no tool
calls), tool_turn (a turn's tool calls), dispatch (tool routing and safety gates), prompts
(what the model is shown), compaction (context budget, compaction, recall), nudges
(stall/loop detectors), and events (what the UI thread receives).
"""

import json
import os
import threading
import time
from typing import Callable

from ..providers import registry
from ..tooling import mcp_client, retrieval
from . import answer, compaction, conversation, planparse, prompts, tool_turn, verify
from .conversation import Conversation, Message
from .events import AgentEvent, EventType, _classify_exc
from ..providers import base as _provider_base
from ..providers.base import _Cancelled, stream_with_retry
from .session import Session, Task, TaskState
from .taskrun import TaskRun, _finish_failed, _finish_stopped, _persist_final

MAX_TURNS = 40          # generous budget for real multi-step tasks
DEFAULT_MODEL = "qwen3.5:9b"
_THINK_LEVELS = frozenset({"off", "on", "low", "medium", "high"})


class _Interrupted(Exception):
    """Raised inside the stream callback when the task's cancel flag is set, so
    an in-flight generation aborts immediately (esc -> stop, not next-turn)."""


def _continuity_effective(session, is_local: bool) -> bool:
    """Whether the conversation thread carries across top-level messages for the current
    model. A user override (`/continuity on|off`) wins; otherwise the provider default —
    cloud continues, local is detached (small local windows fill fast)."""
    override = getattr(session, "continuity_override", None)
    if override is not None:
        return override
    return not is_local


def _reasoning_effective(session) -> str | None:
    """The reasoning level for this turn: session /think override, else TWOB_THINK, else None
    (each provider's capped default). Mirrors _continuity_effective's precedence."""
    override = getattr(session, "think", None)
    if override in _THINK_LEVELS:
        return override
    env = os.environ.get("TWOB_THINK", "").strip().lower()
    return env if env in _THINK_LEVELS else None


def abort_all(session: Session) -> int:
    """Global panic: set the cancel flag on every running task (foreground AND
    backgrounded), clear any pending steer, and close all live HTTP connections so
    parked model calls abort at once. Subprocess tools then die within ~100ms via
    their own cancel poll. Returns how many tasks were aborted."""
    tasks = [t for t in session.tasks
             if t.state in (TaskState.ACTIVE, TaskState.BACKGROUNDED)]
    for t in tasks:
        t.clear_steer()
        t.cancel_flag.set()
    # Looked up via the module (not the re-exported name above) so tests can
    # monkeypatch base.abort_all_connections and have this pick it up live.
    _provider_base.abort_all_connections()
    return len(tasks)


def teardown_helpers() -> None:
    """Hard-stop the long-lived helper servers on esc. Local subprocesses die via
    the cancel flag + process-group kill (see tools._run_cancellable); this tears
    down the rest: LSP servers (they respawn on the next symbol lookup) and MCP
    servers (restarted so their tools survive the session). Best-effort and quiet
    — a helper that's absent or already down is not an error. Runs off the UI
    thread, since MCP shutdown/restart can block on a slow server."""
    try:
        from ..tooling import lsp
        lsp.shutdown_all()
    except Exception:
        pass
    try:
        mcp_client.manager.shutdown()
        mcp_client.manager.start()
    except Exception:
        pass


def pick_default_model() -> str:
    """Default model at startup: prefer local Ollama's qwen3.5:9b if present,
    else the first local model. (Cloud providers aren't auto-defaulted.)"""
    reg = registry.build_registry()
    ol = reg.get("ollama")
    models = []
    if ol is not None:
        try:
            models = ol.list_models()
        except Exception:
            models = []
    if not models:
        raise SystemExit(
            f"No local Ollama models found. Run 'ollama pull {DEFAULT_MODEL}' first, "
            "or configure a cloud provider (set an API key) and pass --model provider:name."
        )
    return DEFAULT_MODEL if DEFAULT_MODEL in models else models[0]



# --- the turn loop -----------------------------------------------------------

_TRACE_LOCK = threading.Lock()


def _traced(on_event: Callable[["AgentEvent"], None], path: str) -> Callable[["AgentEvent"], None]:
    """Tee AgentEvents to a JSONL file (the TWOB_TRACE tap consumed by the eval
    harness) as well as the real sink. Off by default and best-effort — a write
    failure never disturbs the run, and it adds nothing to the model's world. The
    lock keeps whole lines intact if two concurrent worker threads share one path."""
    def tee(ev: "AgentEvent") -> None:
        try:
            line = json.dumps({"t": ev.type.value, "task": ev.task_id, **ev.payload}, default=str)
            with _TRACE_LOCK, open(path, "a") as fh:
                fh.write(line + "\n")
        except Exception:
            pass
        on_event(ev)
    return tee




def _open_run(session: Session, task: Task, on_event: Callable[[AgentEvent], None],
              reg: dict | None) -> TaskRun | None:
    """Resolve the task's model and set up its conversation (continuing the session thread
    or starting fresh, plus recall and retrieval context). None if the model won't resolve —
    the task is then already finished as failed."""
    reg = reg if reg is not None else registry.build_registry()
    model_str = task.model_override or session.default_model
    resolved = registry.resolve(reg, model_str)
    if resolved is None:
        _finish_failed(task, on_event,
                       f"could not resolve model '{model_str}' to a configured provider (try /models)")
        return None
    provider, model = resolved
    # A single read/listing may use ~55% of the model's token budget (≈ tokens*2.2
    # chars). Small local windows get a section suggestion for bigger files; large
    # cloud windows are effectively unbounded.
    read_cap = int(compaction.context_budget(provider, model) * 4 * 0.55)
    # Local models get the constrained git-only tool; cloud (frontier) models get
    # the full shell tool. See toolspec.specs_for / dispatch._dispatch_tool.
    is_local = getattr(provider, "name", "") == "ollama" and getattr(provider, "api_key", None) is None

    if task.conversation is None:
        # Continuity: continue the session's live thread when one exists and continuity is
        # effective for this model (Phase 1: cloud yes, local no); otherwise start fresh.
        # Explicit cwd so the recorded prefix (P10 drift replay) is rebuilt against the same
        # directory even if the process cwd ever diverges from the session's.
        if _continuity_effective(session, is_local) and session.thread is not None:
            task.conversation = session.thread
            fresh_conv = False
        else:
            task.conversation = Conversation(system_prompt=prompts.assemble_system_prompt(cwd=session.cwd))
            fresh_conv = True
    else:
        fresh_conv = False
    conv = task.conversation
    # Register this conversation as the session's live thread so the next top-level message
    # continues it. Ephemeral /tool carriers never reach run_task, so they can't hijack it;
    # detached local runs leave session.thread untouched (each stays its own conversation).
    if _continuity_effective(session, is_local):
        session.thread = conv
    desc = task.description
    if session.read_only:
        desc += ("\n\n(Plan mode is on: do NOT edit or write files. Use the read-only tools to "
                 "investigate, then present a concrete, numbered plan as your final answer.)")
    conv.append(Message.user(desc))
    # If this request points back at earlier work that compaction folded away, pull the
    # referenced turns from the archive and inject them as reference context (P17).
    compaction._maybe_inject_recall(conv, task.id, session.cwd)
    # Dependency-ranked context retrieval: on a fresh conversation, point the model at the
    # files most relevant to this task (host-built, budget-capped, confidence-gated). No-op
    # when disabled, low-confidence, or continuing a thread. Frozen schema untouched.
    if fresh_conv:
        _msg = compaction._retrieval_message(retrieval.retrieve_block(session.cwd, desc))
        if _msg is not None:
            conv.append(_msg)
    return TaskRun(session=session, task=task, on_event=on_event, provider=provider, model=model,
                   is_local=is_local, read_cap=read_cap, conv=conv,
                   first_turn=not any(m.role.value == "assistant" for m in conv.messages))


def _begin_turn(run: TaskRun, poll_perf: bool = True) -> None:
    """Compact if the conversation nears the window, mark the task thinking, announce the turn."""
    compaction._maybe_compact(run.conv, run.provider, run.model, run.task, run.on_event, cwd=run.session.cwd)
    run.task.status_line = "Thinking"
    run.task.turn_started_at = time.monotonic()
    # Best-effort perf readout (local models). May be blank on the very
    # first turn until the model finishes loading; refreshed on the first
    # streamed token below, and persists across turns once set.
    if poll_perf and getattr(run.provider, "name", "") == "ollama" and hasattr(run.provider, "perf"):
        try:
            p = run.provider.perf(run.model)
            if p:
                run.task.perf = p
        except Exception:
            pass
    run.emit(EventType.TURN_START)


def _stream_reply(run: TaskRun, refresh_perf: bool):
    """Stream one model reply to the UI as it arrives. Returns (response, the conversation as
    sent, stream counters {n, thinking}). Raises _Interrupted / _Cancelled when esc stops it."""
    task, provider = run.task, run.provider
    streamed = {"n": 0, "perf": not refresh_perf, "thinking": 0}

    def on_text(chunk: str) -> None:
        if task.cancel_flag.is_set():          # esc pressed mid-stream -> abort now
            raise _Interrupted()
        if not streamed["perf"] and getattr(provider, "name", "") == "ollama" and hasattr(provider, "perf"):
            streamed["perf"] = True
            try:
                val = provider.perf(run.model)
                if val:
                    task.perf = val
            except Exception:
                pass
        streamed["n"] += len(chunk)
        run.emit(EventType.ASSISTANT_DELTA, {"chunk": chunk})

    def on_thinking(chunk: str) -> None:
        if os.environ.get("TWOB_NO_THINK_DISPLAY"):
            return                                    # model still thinks; just not shown
        if task.cancel_flag.is_set():
            raise _Interrupted()
        streamed["thinking"] += len(chunk)
        run.emit(EventType.THINKING_DELTA, {"chunk": chunk})

    active_specs = prompts._active_specs(run.is_local)
    req_conv = run.conv if os.environ.get("TWOB_NO_TRIM") else conversation.trimmed(run.conv)
    resp = stream_with_retry(provider, req_conv, run.model, active_specs, on_text, cancel=task.cancel_flag,
                             reasoning=_reasoning_effective(run.session), on_thinking=on_thinking)
    return resp, req_conv, streamed


def _final_answer_turn(run: TaskRun, loop_broken: bool) -> None:
    """Tool budget exhausted (or the loop breaker fired) — one final turn for a
    best-effort answer (no more tools), so the user gets a summary, not a bare error."""
    task = run.task
    run.conv.append(Message.user(
        ("You've repeated the same action several times without progress. Stop calling "
         "tools now and give your best final answer based on what you already have.")
        if loop_broken else
        ("You've reached the tool-call limit. Give your best final answer now, "
         "based on what you've already found — do not call any more tools.")))
    _begin_turn(run, poll_perf=False)
    try:
        resp, _sent, got = _stream_reply(run, refresh_perf=False)
        planparse.finalize_steps(task.plan_steps)
        task.status_line = ""
        task.state = TaskState.DONE
        _persist_final(run.conv, resp.message)   # keep the final answer in the thread (continuity)
        if got["n"] == 0:
            thinking_shown = got["thinking"] > 0 and (resp.message.thinking or "").strip()
            if not (not (resp.message.text or "").strip() and thinking_shown):
                txt = (resp.message.text or resp.message.thinking or "").strip()
                if not txt:
                    txt = ("(model output was cut off at its length limit)"
                           if resp.done_reason == "length"
                           else "(reached the tool-call limit without a final answer)")
                run.emit(EventType.ASSISTANT_DELTA, {"chunk": txt})
        run.emit(EventType.TASK_DONE)
    except (_Interrupted, _Cancelled):
        run.stopped()
    except Exception as e:
        run.failed(f"max turns reached; final attempt failed: {_classify_exc(e)}")


def run_task(session: Session, task: Task, on_event: Callable[[AgentEvent], None],
             reg: dict | None = None) -> None:
    """Drive the tool-call loop for one task on the calling (worker) thread.
    Resolves the active model to a provider, then drives it via the canonical
    Conversation. Emits events for the UI thread; never writes the terminal.

    Each turn streams one reply, then either handles its answer (answer.handle_answer:
    nudge, verify, or finish) or runs its tool calls (tool_turn.run_tool_calls). A run
    that exhausts MAX_TURNS or trips the loop breaker ends with one best-effort answer."""
    _trace_path = os.environ.get("TWOB_TRACE")
    if _trace_path:
        on_event = _traced(on_event, _trace_path)
    run = _open_run(session, task, on_event, reg)
    if run is None:
        return
    try:
        # The project's real check commands (test/lint), discovered once, to remind a model
        # that can run commands to verify its edits before finishing (see answer.py). Inside the
        # try so even a surprise here lands on the never-throw closure.
        run.repo_checks = verify.discover_or_override(os.getcwd()) if not os.environ.get("TWOB_NO_VERIFY") else []
        # Valid tool names for this task, so coerce_tool_args can let a name nested in
        # a malformed wrapper override an empty/unknown outer name. Fixed for the task.
        run.known_tools = tuple(s.name for s in prompts._active_specs(run.is_local))
        loop_broken = False   # read post-loop to pick the final prompt
        for _ in range(MAX_TURNS):
            if task.cancel_flag.is_set():
                run.stopped()
                return
            _begin_turn(run)
            try:
                resp, req_conv, streamed = _stream_reply(run, refresh_perf=True)
            except (_Interrupted, _Cancelled):
                run.stopped()
                return
            except Exception as e:
                run.failed(_classify_exc(e))
                return

            compaction._calibrate(task, req_conv, resp.prompt_tokens)   # keep the token estimate honest
            msg = resp.message
            content = (msg.text or "").strip()
            if run.first_turn and content:
                parsed = planparse.extract_plan(content)
                if parsed:
                    task.plan_steps = parsed
            run.first_turn = False

            if not msg.tool_calls:
                if answer.handle_answer(run, resp, streamed):
                    continue
                return
            outcome = tool_turn.run_tool_calls(run, msg)
            if outcome == "stopped":
                return
            if outcome == "broken":         # breaker fired — bail to a graceful final answer
                loop_broken = True
                break
        _final_answer_turn(run, loop_broken)
    except (_Interrupted, _Cancelled):
        # Net for any interrupt/cancel that escaped an inner handler — finish quietly,
        # not red. Unreachable today (both stream paths catch these locally), but a
        # panic button must never surface an abort as an error, so the net holds too.
        _finish_stopped(task, on_event)
    except Exception as e:
        # The never-throw guarantee: any exception that escaped the loop body (e.g. a
        # tool dispatch that re-raised) is turned into a clean terminal message here
        # rather than killing the worker thread and hanging the UI on no event.
        _finish_failed(task, on_event, _classify_exc(e))
    finally:
        if task.status_line:
            task.status_line = ""
        # Persist the conversation so this thread can be listed / resumed later.
        # Best-effort and off the model's path (see persist.py); keyed by task id +
        # cwd. Skips trivial conversations. Uses the label model, not the resolved one
        # (which may be unbound if resolution failed early).
        try:
            from ..storage import persist
            persist.save(task.id, session.cwd, task.title,
                         task.model_override or session.default_model, task.conversation)
        except Exception:
            pass
