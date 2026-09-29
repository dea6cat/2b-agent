"""Host-side detectors for a stuck or misbehaving model: the repeat-call loop guard, the nudges
fed back when a turn narrates instead of acting, and the verify-round verdict. Pure — no I/O."""
import collections
import hashlib
import json
import re


# Volatile substrings stripped from a tool result before hashing it for the loop
# signature, so a result that's identical *except* for a changing timestamp, run
# duration, or hash still counts as "the same" — the exact case that let a repeated,
# genuinely-stuck call (e.g. a test rerun whose only difference is "in 1.23s") slip
# past a whole-result hash. Kept deliberately narrow (clock/ISO times, durations,
# long hex ids/addresses) so distinct failures — which differ in real text like a
# test name — never collapse together.
_VOLATILE_PATTERNS = [
    re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"),   # ISO date-time
    re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\b"),           # clock HH:MM:SS
    re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|secs|seconds|min|mins|minutes)\b"),  # durations
    # Long hex ids / hashes (git SHAs, uuids). The lookahead requires at least one
    # a–f letter so a plain long DECIMAL (a byte count, epoch-ms, PID) isn't mistaken
    # for a hash and stripped — that would collapse genuinely-different numeric output.
    re.compile(r"\b(?=[0-9a-f]{12,40}\b)[0-9a-f]*[a-f][0-9a-f]*\b"),
    re.compile(r"0x[0-9a-fA-F]+"),                               # hex addresses
]


def _strip_volatile(text: str) -> str:
    for rx in _VOLATILE_PATTERNS:
        text = rx.sub("~", text)
    return text


class _LoopGuard:
    """Detects a model stuck repeating the same tool call with no progress and
    graduates the response, so a stall degrades gracefully instead of hard-stopping:

      warn   (a signature first reaches `warn_at`)   -> nudge the model to change tack
      veto   (it reaches `veto_at`)                  -> substitute a corrective result
      breaker(`breaker_vetoes` vetoes accumulate)    -> stop tools, draft a final answer

    Host-side and model-agnostic. The no-progress signature hashes the tool *result*
    with volatile fields (times/durations/ids) stripped, so an identical-but-for-a-
    timestamp repeat still trips, while distinct results (real progress) do not."""

    def __init__(self, window: int = 10, warn_at: int = 3, veto_at: int = 5,
                 breaker_vetoes: int = 3):
        self._recent: collections.deque[str] = collections.deque(maxlen=window)
        self.warn_at, self.veto_at, self.breaker_vetoes = warn_at, veto_at, breaker_vetoes
        self._warned: set[str] = set()
        self._vetoes = 0

    @staticmethod
    def _sig(name: str, args: dict, result: str) -> str:
        # Hash the WHOLE result (volatile fields stripped), not just line 1:
        # run_command/run_git failures all start "error: command exited 1", so a
        # first-line key would collapse every distinct test failure into one and
        # falsely trip a fix→rerun→fix loop. A genuinely-stuck repeat still hashes equal.
        body = hashlib.sha1(_strip_volatile(result or "").encode("utf-8", "replace")).hexdigest()[:16]
        # Put `path` first so it survives truncation even when a large old_text/new_text
        # would otherwise push it past the arg-string cap and collide across files.
        path = args.get("path", "") if isinstance(args, dict) else ""
        return f"{name}|{path}|{json.dumps(args, sort_keys=True, default=str)[:150]}|{body}"

    def record(self, name: str, args: dict, result: str) -> str:
        sig = self._sig(name, args, result)
        self._recent.append(sig)
        self._warned &= set(self._recent)   # forget signatures that aged out of the window
        n = self._recent.count(sig)
        if n >= self.veto_at:
            # _vetoes is a task-lifetime count (never reset), so vetoes on different
            # signatures still add up to the breaker. Intended: a run that stalls this
            # hard three separate times is genuinely struggling, and the breaker only
            # degrades to a graceful final answer — not a hard failure.
            self._vetoes += 1
            return "breaker" if self._vetoes >= self.breaker_vetoes else "veto"
        if n >= self.warn_at and sig not in self._warned:
            self._warned.add(sig)
            return "warn"
        return ""


_LOOP_NUDGE = (
    "You've made the same tool call and gotten the same result several times — that "
    "approach isn't working, so stop repeating it. If an edit_file old_text isn't "
    "matching, read_file the file again and copy the exact text (including indentation) "
    "from what you see; otherwise try a genuinely different approach."
)

# Substituted in place of a vetoed repeat's real result, so the model sees a correction
# instead of the same output yet again (and role-alternation/tool pairing is preserved).
_LOOP_VETO = (
    "[blocked] You've made this exact call repeatedly with the same result and no progress. "
    "Its output is unchanged from the earlier attempts above — stop repeating it. Try a "
    "materially different approach (re-read the file and copy the exact text, edit a "
    "different location, or use a different tool). If you're genuinely stuck, say so and "
    "give your best answer from what you already have."
)

# Substituted on the breaker step, right before the loop bails to a final-answer turn.
_LOOP_BREAKER = (
    "[stopped repeating] This action kept repeating without progress and has been halted. "
    "No more tools will run — give your best final answer from what you already have."
)

# A small model sometimes narrates a tool call it never makes ("I'll use edit_file to
# …") and ends its turn — the task "completes" with nothing done. We detect a final
# answer that names a frozen tool in first-person future-intent phrasing but carried no
# tool call, and nudge the model to actually make the call. Requiring a LITERAL tool
# name keeps this from firing on ordinary prose ("I'll add a note").
_TOOL_NAMES = ("edit_file", "write_file", "read_file", "search_files", "list_files",
               "run_git", "run_command")
_INTENT_RE = re.compile(r"\b(i['’]?ll|i will|i['’]?m going to|i['’]?m about to|let me|"
                        r"going to|i need to|i can now)\b", re.IGNORECASE)   # bare "I can" describes ability, not intent

_STEER_MARKER = (
    "\n\n[user steer — the user sent this while the turn was running; it is their latest, "
    "highest-priority instruction. Adjust course to follow it]:\n"
)

_PROMISE_NUDGE = (
    "You described a tool call but didn't actually make one. Don't just describe the "
    "change — make the tool call now to perform it (e.g. call edit_file with the exact "
    "old_text/new_text). If the work is genuinely already done, say so plainly without "
    "naming a tool."
)


def _promised_tool_but_didnt(text: str) -> bool:
    """True if a final answer (no tool calls this turn) names a frozen tool in
    first-person, future-intent phrasing — i.e. the model said it would act but didn't.
    Past tense ('I edited …', 'used edit_file') doesn't match, so a genuine done-report
    isn't flagged."""
    if not text:
        return False
    low = text.lower()
    if not any(t in low for t in _TOOL_NAMES):
        return False
    return bool(_INTENT_RE.search(text))


_STALL_NUDGE = (
    "You described what you intend to do but didn't use any tool. Investigate or act with a "
    "tool now (list_files, read_file, search_files, edit_file, …) — don't only narrate the plan. "
    "If you already have the answer, give it plainly without describing steps."
)

_STALL_RE = re.compile(
    r"(i['’]?ll|i will|let me|going to|i need to|voy a|d[eé]jame)"
    r"[^.!?]{0,40}?\b(explore|look|check|read|search|"
    r"examine|find|list|investigate|start by|see what)",
    re.IGNORECASE)


def _stalled_without_acting(text: str) -> bool:
    """True if a no-tool-call turn narrates an intent to investigate/act ('let me first
    explore…', 'I'll read…') rather than delivering an answer. Requires an intent opener
    followed by an investigative verb, so ordinary sign-offs ('let me know if…') and
    done-reports ('I can now confirm…') don't match. Caller gates on zero tool calls so far."""
    return bool(text) and bool(_STALL_RE.search(text))


_CLARIFY_NUDGE = (
    "Don't ask the user to clarify before you've looked. Use the read-only tools "
    "(list_files, search_files, read_file) to answer your own questions from the code, then "
    "act. Only ask the user if you're still genuinely blocked after investigating."
)

# A small model faced with an actionable request sometimes punts with a wall of
# clarifying questions ("Could you specify which files… what should it accomplish?")
# instead of just looking. We detect a no-tool-call turn that solicits clarification and
# nudge it to investigate first. Requires a real '?' plus a clarification-request phrase,
# so a genuine answer that ends with a courtesy offer ("want me to add tests?") is spared.
_CLARIFY_RE = re.compile(
    r"("
    r"need (?:a bit )?more (?:detail|info|information|context|clarit|specific)"
    r"|(?:could|can) you (?:please )?(?:specify|clarify|provide|share|tell me|elaborate|confirm|describe|let me know)"
    r"|please (?:specify|clarify|confirm|describe|elaborate|let me know|provide)"
    r"|to know (?:exactly )?what you"
    r"|what (?:would|do) you (?:want|like|mean|expect|have in mind)"
    r"|which (?:file|files|function|method|class|module|component|directory|folder|part of)"
    r"|necesito m[aá]s (?:detalle|informaci[oó]n|contexto)"
    r"|podr[ií]as (?:especificar|aclarar|indicar|decirme|proporcionar)"
    r")",
    re.IGNORECASE)


def _asked_instead_of_acting(text: str) -> bool:
    """True if a no-tool-call turn asks the user to clarify the task ('could you specify
    which files…', 'I need more detail…') instead of investigating first. Requires an
    actual question mark plus a clarification-request phrase, so a plain answer that ends
    with a courtesy offer ('want me to add tests?') isn't flagged. Caller gates on zero
    tool calls so far — the fix is to look with the read-only tools, then ask only if
    still genuinely blocked."""
    return bool(text) and "?" in text and bool(_CLARIFY_RE.search(text))


_FALSE_DONE_NUDGE = (
    "Your edits did NOT apply — every edit_file/write_file this task returned an error "
    "(see the results above), so no file was changed. Do not report this as done. Use "
    "read_file to find the correct path and the exact text to match, then retry the edit; "
    "only finish once an edit actually succeeds."
)


def _edits_all_failed(edit_attempts: int, applied_count: int) -> bool:
    """True when the model tried to edit files but none landed — every edit_file/write_file
    this task returned an error, so nothing changed. `applied_count` is len(task.edit_history),
    which is appended only on a successful write/edit (see apply_write/apply_edit). Used to
    stop a model (a local-model failure mode) from declaring success on edits that never
    applied. False when no edit was attempted, or at least one applied."""
    return edit_attempts > 0 and applied_count == 0


def _verify_to_run(checks, fast_only: bool):
    """The checks to run this round — drop the test tier under TWOB_VERIFY_FAST."""
    return [(c, k) for c, k in checks if not (fast_only and k == "tests")]


def _verify_verdict(results) -> str:
    """'cancelled' if any check was aborted (ESC — stop the task), else 'fail' if any failed,
    else 'pass' (only passes/skips)."""
    if any(r.status == "cancelled" for r in results):
        return "cancelled"
    if any(r.status == "fail" for r in results):
        return "fail"
    return "pass"
