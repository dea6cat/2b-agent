"""Execute one model turn's tool calls: coerce malformed call shapes, run a batch of pure reads
concurrently or each call in order, apply the loop guard's verdict to every result, and fold in
any steer the user typed mid-turn."""
from ..tooling import tools
from . import dispatch, nudges, planparse
from .conversation import Message, ToolResult
from .events import EventType
from .taskrun import TaskRun

_STATUS = {
    "list_files": "Listing files",
    "read_file": "Reading",
    "search_files": "Searching",
    "edit_file": "Editing",
    "write_file": "Writing",
}


def run_tool_calls(run: TaskRun, msg) -> str:
    """Dispatch every call in `msg` and append the results turn. Returns 'stopped' when the
    task was stopped mid-batch (already finished — the caller just returns), 'broken' when the
    loop breaker fired (the caller bails to a best-effort final answer), else ''."""
    task = run.task
    run.conv.append(msg)
    calls = msg.tool_calls
    # Normalize each malformed-but-recoverable call shape (stringified args, args
    # nested under an "arguments" key, name only inside the wrapper) up front — so
    # classification, display, plan inference, dispatch, and the loop-guard all see
    # the same coerced values that actually ran.
    for tc in calls:
        tc.name, tc.arguments = tools.coerce_tool_args(tc.name, tc.arguments, run.known_tools)
    run.tool_calls_made += len(calls)
    run.edit_attempts += sum(1 for tc in calls if tc.name in ("edit_file", "write_file"))
    results = []
    nudge_pending = False
    loop_broken = False

    def _emit_start(tc):
        planparse.infer_active_step(task.plan_steps, tc.name, tc.arguments)
        task.status_line = _STATUS.get(tc.name, "Working")
        shown = {k: (v if k != "content" else f"<{len(v)} chars>") for k, v in tc.arguments.items()}
        run.emit(EventType.TOOL_CALL_START, {"name": tc.name, "shown": shown})

    def _record(tc, result) -> str:
        run.emit(EventType.TOOL_CALL_RESULT, {"name": tc.name, "result": result})
        results.append(ToolResult(tool_call_id=tc.id, content=result))
        return run.loop_guard.record(tc.name, tc.arguments, result)

    def _apply_loop_verdict(tc, verdict) -> bool:
        """Act on the loop-guard's graduated verdict for the just-recorded result.
        warn -> nudge; veto -> substitute a corrective result; breaker -> substitute
        and signal a bail to a graceful final answer. Returns True on breaker."""
        nonlocal nudge_pending
        if verdict == "warn":
            nudge_pending = True
        elif verdict == "veto":
            results[-1].content = nudges._LOOP_VETO
            run.emit(EventType.LOG, {"text": f"Repeated {tc.name} vetoed — no progress; asking for a different approach."})
        elif verdict == "breaker":
            results[-1].content = nudges._LOOP_BREAKER
            run.emit(EventType.LOG, {"text": f"Loop breaker: {tc.name} kept repeating — drafting a best-effort answer."})
            return True
        return False

    # Fast path: when the whole batch is side-effect-free reads, run their I/O
    # concurrently (the biggest speed lever on multi-read/-search turns), then emit
    # each start/result pair in order so the single-slot TUI tool line stays correct.
    if len(calls) > 1 and all(dispatch._is_parallel_read(c.name, c.arguments) for c in calls):
        if task.cancel_flag.is_set():
            run.stopped()
            return "stopped"
        task.status_line = "Reading"
        computed = dispatch._run_reads_concurrently(run.session, task, calls, run.read_cap)
        task.last_read_arg = None            # a multi-read batch isn't a single-file loop
        task.read_repeat = 0
        for tc, result in zip(calls, computed):
            if task.cancel_flag.is_set():
                run.stopped()
                return "stopped"
            _emit_start(tc)
            if _apply_loop_verdict(tc, _record(tc, result)):
                loop_broken = True   # finish the batch (every call needs a result), then bail
    else:
        for tc in calls:
            if task.cancel_flag.is_set():        # esc while tools are running
                run.stopped()
                return "stopped"
            _emit_start(tc)
            try:
                result = dispatch._dispatch_tool(run.session, task, tc.name, tc.arguments, run.read_cap)
            except Exception as e:
                # esc can tear a tool's helper (LSP/MCP) out from under it mid-call;
                # when cancelled, finish quietly rather than surfacing that as an error.
                # Log the exception first so an *unrelated* failure that merely
                # coincided with the stop isn't lost without a trace.
                if task.cancel_flag.is_set():
                    run.emit(EventType.LOG, {"text": f"(stopped while {tc.name} was running: {e})"})
                    run.stopped()
                    return "stopped"
                raise
            if _apply_loop_verdict(tc, _record(tc, result)):
                loop_broken = True   # finish the batch (every call needs a result), then bail
    # Steer: fold any text the user typed mid-turn into the last tool result the
    # model will read next, marked as their latest instruction. Appending to a tool
    # result (rather than adding a user message) preserves role alternation and the
    # tool_call↔result pairing every provider needs. Consumed only when there's a
    # result to carry it; otherwise it stays buffered for the UI to handle at finish.
    steer = task.take_steer()
    if steer and results:
        results[-1].content += nudges._STEER_MARKER + steer
    run.conv.append(Message.results(results))
    if nudge_pending and not loop_broken:   # on a breaker, the final-answer prompt supersedes the nudge
        run.conv.append(Message.user(nudges._LOOP_NUDGE))
    return "broken" if loop_broken else ""
