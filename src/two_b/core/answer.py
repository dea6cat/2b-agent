"""Handle a turn that ended with no tool calls: nudge a model that talked instead of acting, run
the project's checks on its edits (a bounded verify-and-fix loop), refuse a false "done", and
otherwise finish the task with its answer."""
import os

from . import nudges, planparse, untrusted, verify
from .conversation import Message
from .events import EventType
from .session import TaskState
from .taskrun import TaskRun, _persist_final

MAX_VERIFY_ROUNDS = 2   # bound on host-run verify-and-fix rounds per task


def handle_answer(run: TaskRun, resp, streamed: dict) -> bool:
    """True when the model gets another turn (nudged, or handed failing checks to fix);
    False when the task is over — finished with its answer, or stopped during verify."""
    msg = resp.message
    content = (msg.text or "").strip()
    nudge = _stall_nudge(run, content)
    if nudge:
        run.conv.append(msg)
        run.conv.append(Message.user(nudge))
        return True
    outcome = _verify_edits(run, msg, content)
    if outcome:
        return outcome == "fix"
    # Declared done, but every edit this task attempted errored (edit_history is
    # empty despite edit_file/write_file calls) — nothing was changed. Stop the
    # false "done" (a local-model failure mode) once, pointing back at the errors so
    # it retries for real. Fires for local and cloud alike.
    if (content and not run.false_done_nudged
            and nudges._edits_all_failed(run.edit_attempts, len(run.task.edit_history))):
        run.false_done_nudged = True
        run.conv.append(msg)
        run.conv.append(Message.user(nudges._FALSE_DONE_NUDGE))
        return True
    _finish_done(run, resp, content, streamed)
    return False


def _stall_nudge(run: TaskRun, content: str) -> str:
    """The nudge for a turn that talked instead of acting, or '' when it's a real answer.
    Each kind is bounded per task, and its counter is advanced here when it fires."""
    # Caught the model narrating a tool call it never made — give it another
    # turn to actually do it (bounded, so a model that just keeps talking
    # still finalizes rather than looping).
    if run.promise_nudges < 2 and nudges._promised_tool_but_didnt(content):
        run.promise_nudges += 1
        return nudges._PROMISE_NUDGE
    # A no-tool-call turn that only narrates intent, with zero actions taken so far,
    # is a stall (measured on qwen3.5:9b) — nudge once to actually use a tool. Bounded,
    # and gated on tool_calls_made==0 so a real final answer is never nudged.
    if (run.stall_nudges < 1 and run.tool_calls_made == 0
            and not nudges._promised_tool_but_didnt(content)
            and nudges._stalled_without_acting(content)):
        run.stall_nudges += 1
        return nudges._STALL_NUDGE
    # Asked the user to clarify without looking first — a stall dressed as a
    # question (seen on local models). Nudge once to investigate before asking.
    if (run.clarify_nudges < 1 and run.tool_calls_made == 0
            and not nudges._promised_tool_but_didnt(content)
            and not nudges._stalled_without_acting(content)
            and nudges._asked_instead_of_acting(content)):
        run.clarify_nudges += 1
        return nudges._CLARIFY_NUDGE
    return ""


def _verify_edits(run: TaskRun, msg, content: str) -> str:
    """Host-run verify-and-fix: the model finished with edits that landed — run the
    project's own checks and, on failure, feed the errors back for a bounded fix
    loop. The HOST runs them (not the model), so local models get toolchain
    grounding without run_command. Replaces the old cloud-only verify nudge.

    Returns 'fix' (failures fed back — give the model another turn), 'stopped' (cancelled
    mid-check — the task is already finished), or '' (passed, skipped, or out of rounds)."""
    task = run.task
    if not (content and run.repo_checks and task.edit_history):
        return ""
    to_run = nudges._verify_to_run(run.repo_checks, bool(os.environ.get("TWOB_VERIFY_FAST")))
    if not to_run:
        return ""
    task.status_line = "Verifying"
    results = verify.run_checks(
        to_run, cancel=task.cancel_flag,
        on_start=lambda c: run.emit(EventType.LOG, {"text": f"Verifying — running {c}…"}))
    verdict = nudges._verify_verdict(results)
    if verdict == "cancelled" or task.cancel_flag.is_set():
        run.stopped()
        return "stopped"
    if verdict != "fail":
        ran = [r.cmd for r in results if r.status == "pass"]
        if ran:
            run.emit(EventType.LOG, {"text": "✓ checks passed: " + ", ".join(ran)})
        return ""
    failures = [r for r in results if r.status == "fail"]
    if run.verify_rounds >= MAX_VERIFY_ROUNDS:
        # Exhausted the fix budget — finish, but report the true state.
        run.emit(EventType.LOG, {"text":
            "⚠ checks still failing after "
            f"{MAX_VERIFY_ROUNDS} fix attempt(s): "
            + ", ".join(r.cmd for r in failures)})
        return ""
    run.verify_rounds += 1
    body = "\n\n".join(
        f"`{r.cmd}` failed:\n" + untrusted.wrap(r.output, f"check:{r.cmd}")
        for r in failures)
    run.conv.append(msg)
    run.conv.append(Message.user(
        "Your edits did not pass the project checks. Fix the code so "
        "these pass, then finish:\n\n" + body))
    run.emit(EventType.LOG, {"text": f"{len(failures)} check(s) failed — fixing…"})
    return "fix"


def _finish_done(run: TaskRun, resp, content: str, streamed: dict) -> None:
    """Close the task with the model's answer, emitting it now if nothing streamed."""
    task, msg = run.task, resp.message
    planparse.finalize_steps(task.plan_steps)
    task.status_line = ""
    task.state = TaskState.DONE
    _persist_final(run.conv, msg)   # keep the final answer in the thread (continuity)
    # If nothing streamed (e.g. answer landed in `thinking`), emit it now — unless the
    # thinking was already shown live, in which case it stands as the visible output.
    if streamed["n"] == 0:
        thinking_shown = streamed["thinking"] > 0 and (msg.thinking or "").strip()
        if not (not content and thinking_shown):
            fallback = content or (msg.thinking or "").strip()
            if not fallback:
                # No content and no call. Name the cause instead of re-prompting
                # the same wall: a length/truncation stop is a distinct, reportable
                # condition, not a genuine empty answer.
                fallback = ("(model output was cut off at its length limit)"
                            if resp.done_reason == "length"
                            else "(model returned an empty response)")
            run.emit(EventType.ASSISTANT_DELTA, {"chunk": fallback})
    run.emit(EventType.TASK_DONE)
