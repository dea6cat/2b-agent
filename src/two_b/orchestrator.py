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
"""
import collections
import hashlib
import io
import json
import os
import re
import threading
import time
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from . import catalog, conversation, diagnostics, mcp_client, planparse, registry, tools
from .conversation import Conversation, Message, Role, ToolResult
from .providers.base import ProviderError, stream_with_retry
from .session import PendingConfirmation, Session, Task, TaskState
from .toolspec import TOOL_SPECS, specs_for, DELEGATE_SPEC

MAX_TURNS = 40          # generous budget for real multi-step tasks
DEFAULT_MODEL = "qwen3.5:9b"

# --- context-window management (auto-compaction) -----------------------------
# Estimated-token budgets per provider. Local models run small windows, so we
# compact aggressively there; cloud models have far more headroom. Override the
# local budget with TWOB_CONTEXT_TOKENS if your Ollama num_ctx is larger.
CONTEXT_BUDGETS = {
    "ollama": 8000, "anthropic": 180000, "openai": 120000,
    "openrouter": 120000, "mistral": 120000, "nvidia": 120000, "google": 900000,
}
COMPACT_AT = 0.75       # compact once estimated usage crosses this fraction
COMPACT_KEEP_TAIL = 6   # most-recent messages kept verbatim (rest are summarized)
_COMPACT_MAX_INPUT_CHARS = 48_000   # cap the transcript handed to the summarizer

COMPACT_SYSTEM = (
    "You are compressing a coding session's history to free up context so work "
    "can continue uninterrupted. Summarize concisely but completely: the user's "
    "goal, files and code already read and what they contain, findings, decisions "
    "made, edits applied (with exact file paths), and what remains to be done. "
    "Preserve exact identifiers, paths, and any values needed to keep working. "
    "Output plain text only — no preamble."
)

BASE_SYSTEM_PROMPT = (
    "You are a careful coding assistant with file tools (list_files, read_file, search_files, "
    "edit_file, write_file) and a command tool (run_git for version control; run_command for "
    "shell commands like tests and builds, when available). Explore before answering or editing — use "
    "search_files to find where something is defined or used instead of guessing paths. "
    "For changes to existing files, prefer edit_file (an exact old_text/new_text "
    "replacement) over write_file — it's faster and safer, especially on large files. "
    "Only use write_file for new files or small existing ones. Paths may be relative to the "
    "working directory or absolute — pass them through unchanged. If a tool returns an error, "
    "report it plainly; never substitute a different file or invent a file's location or contents. "
    "Reply in the same language the user writes in. "
    "When finished, reply with a plain-text final answer and make no further tool calls."
)

# Argument shapes for the frozen tools, stated once. Small models otherwise nest
# args under an "arguments" key or use the wrong key name; coerce_tool_args
# recovers many of those host-side, but stating the exact flat shape up front
# cuts the malformed calls that need recovering in the first place.
TOOL_ARG_HINT = (
    "\n\nCall each tool with a flat JSON object using exactly these argument names — "
    "do not nest them under an \"arguments\" key:\n"
    "  list_files{path}\n"
    "  read_file{path}\n"
    "  search_files{query, path}\n"
    "  edit_file{path, old_text, new_text}\n"
    "  write_file{path, content}\n"
    "  run_git{args}\n"
    "  run_command{command}"
)
SYSTEM_PROMPT = BASE_SYSTEM_PROMPT + TOOL_ARG_HINT + planparse.PLAN_PROMPT

_STATUS = {
    "list_files": "Listing files",
    "read_file": "Reading",
    "search_files": "Searching",
    "edit_file": "Editing",
    "write_file": "Writing",
}


class EventType(Enum):
    TURN_START = "turn_start"
    ASSISTANT_DELTA = "assistant_delta"    # a streamed chunk of the reply
    ASSISTANT_TEXT = "assistant_text"      # (legacy; kept for non-stream callers)
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_RESULT = "tool_call_result"
    LOG = "log"                # captured tool stdout, to print to scrollback
    TASK_DONE = "task_done"
    TASK_ERROR = "task_error"


@dataclass
class AgentEvent:
    type: EventType
    task_id: str
    payload: dict[str, Any] = field(default_factory=dict)


class _Interrupted(Exception):
    """Raised inside the stream callback when the task's cancel flag is set, so
    an in-flight generation aborts immediately (esc -> stop, not next-turn)."""


def _finish_stopped(task: Task, on_event: Callable[["AgentEvent"], None]) -> None:
    """Return a task to idle after the user stops it — commit whatever streamed,
    show a quiet 'Stopped.' line, no red error."""
    task.status_line = ""
    task.state = TaskState.ERROR
    task.error = "stopped"
    on_event(AgentEvent(EventType.LOG, task.id, {"text": "Stopped."}))
    on_event(AgentEvent(EventType.TASK_DONE, task.id))


def _finish_failed(task: Task, on_event: Callable[["AgentEvent"], None], reason: str) -> None:
    """Terminal failure closure. Guarantees run_task ends with a clean, non-empty
    final message — never a bare stack trace, an empty output, or (worse) an
    exception escaping the worker thread and leaving the UI waiting forever. The
    reason is always classified to a readable, non-empty string before it goes out."""
    reason = (reason or "").strip() or "unknown error"
    task.status_line = ""
    task.state = TaskState.ERROR
    task.error = reason
    on_event(AgentEvent(EventType.TASK_ERROR, task.id, {"error": reason}))


def _classify_exc(e: BaseException) -> str:
    """A readable one-line reason for an otherwise-opaque exception. ProviderError
    already carries a '[provider] message'; for everything else, name the type so a
    blank-message exception (e.g. KeyError('path')) never surfaces as empty output."""
    if isinstance(e, ProviderError):
        return str(e)
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


class _LoopGuard:
    """Detects a model stuck repeating the same tool call with the same result —
    e.g. re-submitting an edit whose old_text keeps not matching (a real failure
    mode: without this the loop runs until the tool-call budget or a timeout).
    Host-side and model-agnostic. record() returns 'nudge' the first time a
    signature reaches `nudge_at`, 'stop' once it reaches `stop_at`, else ''."""

    def __init__(self, window: int = 10, nudge_at: int = 3, stop_at: int = 5):
        self._recent: collections.deque[str] = collections.deque(maxlen=window)
        self.nudge_at, self.stop_at = nudge_at, stop_at
        self._nudged: set[str] = set()

    @staticmethod
    def _sig(name: str, args: dict, result: str) -> str:
        # Hash the WHOLE result, not just line 1: run_command/run_git failures all
        # start "error: command exited 1", so a first-line key would collapse every
        # distinct test failure into one and falsely stop a fix→rerun→fix loop. A
        # genuinely-stuck repeat (same edit, same "not found" hint) still hashes equal.
        body = hashlib.sha1((result or "").encode("utf-8", "replace")).hexdigest()[:16]
        # Put `path` first so it survives truncation even when a large old_text/new_text
        # would otherwise push it past the arg-string cap and collide across files.
        path = args.get("path", "") if isinstance(args, dict) else ""
        return f"{name}|{path}|{json.dumps(args, sort_keys=True, default=str)[:150]}|{body}"

    def record(self, name: str, args: dict, result: str) -> str:
        sig = self._sig(name, args, result)
        self._recent.append(sig)
        self._nudged &= set(self._recent)   # forget signatures that aged out of the window
        n = self._recent.count(sig)
        if n >= self.stop_at:
            return "stop"
        if n >= self.nudge_at and sig not in self._nudged:
            self._nudged.add(sig)
            return "nudge"
        return ""


_LOOP_NUDGE = (
    "You've made the same tool call and gotten the same result several times — that "
    "approach isn't working, so stop repeating it. If an edit_file old_text isn't "
    "matching, read_file the file again and copy the exact text (including indentation) "
    "from what you see; otherwise try a genuinely different approach."
)

# A small model sometimes narrates a tool call it never makes ("I'll use edit_file to
# …") and ends its turn — the task "completes" with nothing done. We detect a final
# answer that names a frozen tool in first-person future-intent phrasing but carried no
# tool call, and nudge the model to actually make the call. Requiring a LITERAL tool
# name keeps this from firing on ordinary prose ("I'll add a note").
_TOOL_NAMES = ("edit_file", "write_file", "read_file", "search_files", "list_files",
               "run_git", "run_command")
_INTENT_RE = re.compile(r"\b(i['’]?ll|i will|i['’]?m going to|i['’]?m about to|let me|"
                        r"going to|i need to|i can (?:now )?)\b", re.IGNORECASE)

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


def teardown_helpers() -> None:
    """Hard-stop the long-lived helper servers on esc. Local subprocesses die via
    the cancel flag + process-group kill (see tools._run_cancellable); this tears
    down the rest: LSP servers (they respawn on the next symbol lookup) and MCP
    servers (restarted so their tools survive the session). Best-effort and quiet
    — a helper that's absent or already down is not an error. Runs off the UI
    thread, since MCP shutdown/restart can block on a slow server."""
    try:
        from . import lsp
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


PROJECT_DOC_MAX = 2800   # cap on the /init 2B.md folded into the system prompt
MCP_LOCAL_CAP = 6        # max MCP tools shown to a local model (protect its context/focus)


def _project_context() -> str:
    """The /init project map (2B.md), capped, to orient the model up front — so it
    knows the layout instead of hunting for files. AGENTS.md is a fallback."""
    for name in ("2B.md", "AGENTS.md"):
        path = os.path.join(os.getcwd(), name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", errors="replace") as f:
                doc = f.read().strip()
        except OSError:
            continue
        if doc:
            return doc[:PROJECT_DOC_MAX] + ("\n… [truncated]" if len(doc) > PROJECT_DOC_MAX else "")
    return ""


def _active_specs(is_local: bool):
    """Base file tools + the model's exec tool + curated MCP tools. Local models
    get a small MCP cap so a big enabled set can't flood their tool list.
    delegate (fan-out to sub-agents) is exposed to cloud models only."""
    mcp = mcp_client.manager.tool_specs()
    if is_local:
        mcp = mcp[:MCP_LOCAL_CAP]
    base = specs_for(is_local) + mcp
    return base if is_local else base + (DELEGATE_SPEC,)


def context_budget(provider, model: str) -> int:
    """Token budget for a provider/model. Ollama (local or cloud) sizes its own
    window — local from the model's trained max capped to what RAM allows (or
    TWOB_CONTEXT_TOKENS), cloud a fixed large window. For cloud providers the
    per-model catalog gives the model's real window; unknown models fall back to
    the coarse per-provider constant, so the budget matches reality either way."""
    name = getattr(provider, "name", "")
    if name.startswith("ollama") and hasattr(provider, "context_window"):
        try:
            return provider.context_window(model)
        except Exception:
            pass
    win = catalog.context_window(model)
    if win:
        return win
    return CONTEXT_BUDGETS.get(name, 8000)


def context_usage(used: int, budget: int) -> tuple[int, bool]:
    """Percent of the context window used, and whether it's in the warning zone (>=80%).
    Small local windows fill fast, so surfacing this is the point — see the TUI meter.
    Returns (0, False) when the budget is unknown."""
    if budget <= 0:
        return 0, False
    pct = min(100, round(used * 100 / budget))
    return pct, pct >= 80


def estimate_tokens(conv: Conversation) -> int:
    """Rough token estimate for a conversation (~4 chars/token). Cheap and
    provider-agnostic — good enough to decide when to compact."""
    total = len(conv.system_prompt or "")
    for m in conv.messages:
        total += len(m.text or "") + len(m.thinking or "")
        for tc in m.tool_calls:
            total += len(tc.name) + len(str(tc.arguments))
        for r in m.tool_results:
            total += len(r.content or "")
    return total // 4


def _render_transcript(messages: list[Message]) -> str:
    """Flatten history to a plain-text transcript for the summarizer."""
    parts: list[str] = []
    for m in messages:
        if m.role == Role.USER and m.tool_results:
            for r in m.tool_results:
                parts.append(f"[tool result]\n{r.content}")
        elif m.role == Role.USER:
            parts.append(f"[user]\n{m.text or ''}")
        elif m.role == Role.ASSISTANT:
            if m.thinking:
                parts.append(f"[assistant reasoning]\n{m.thinking}")
            if m.text:
                parts.append(f"[assistant]\n{m.text}")
            for tc in m.tool_calls:
                parts.append(f"[assistant called {tc.name}] {tc.arguments}")
    text = "\n\n".join(parts)
    if len(text) > _COMPACT_MAX_INPUT_CHARS:      # keep the most-recent portion
        text = "…[earlier turns elided]…\n\n" + text[-_COMPACT_MAX_INPUT_CHARS:]
    return text


def compact_conversation(conv: Conversation, provider, model: str) -> bool:
    """Replace all but the recent tail of `conv` with a single summary message.
    Returns True if compaction happened. The cut lands on an assistant message so
    tool_call/tool_result pairs in the kept tail stay intact for every provider."""
    msgs = conv.messages
    target = max(0, len(msgs) - COMPACT_KEEP_TAIL)
    # Largest assistant-boundary cut at/below the keep-tail target; if the tail
    # would swallow everything (few messages), fall back to the last group so we
    # still fold the rest rather than giving up.
    cut = next((i for i in range(target, 0, -1) if msgs[i].role == Role.ASSISTANT), None)
    if not cut:
        cut = next((i for i in range(1, len(msgs)) if msgs[i].role == Role.ASSISTANT), None)
    if not cut:                                   # nothing safe/worthwhile to fold
        return False
    head, tail = msgs[:cut], msgs[cut:]
    summ = Conversation(system_prompt=COMPACT_SYSTEM)
    summ.append(Message.user(_render_transcript(head)))
    buf: list[str] = []
    resp = provider.stream(summ, model, (), lambda c: buf.append(c))
    summary = "".join(buf).strip() or (resp.message.text or resp.message.thinking or "").strip()
    if not summary:
        return False
    recap = Message.user("[Summary of earlier conversation, compacted to save context]\n\n" + summary)
    conv.messages = [recap] + tail
    return True


def _maybe_compact(conv: Conversation, provider, model: str, task: Task,
                   on_event: Callable[["AgentEvent"], None]) -> None:
    """Compact `conv` in place when it nears the model's context budget. Failures
    are swallowed — a task must never break because compaction couldn't run."""
    try:
        budget = context_budget(provider, model)
        est = estimate_tokens(conv)
        if est < int(budget * COMPACT_AT):
            return
        # Anti-thrash: if we just compacted and nothing meaningful was added since,
        # don't compact again — a single oversized recent result can't be folded
        # away, and re-running it every turn is pointless churn.
        if task.last_compact_tokens and est <= int(task.last_compact_tokens * 1.15):
            return
        task.status_line = "Compacting conversation"
        task.turn_started_at = time.monotonic()
        on_event(AgentEvent(EventType.LOG, task.id,
                            {"text": "Nearing context limit — compacting conversation to keep going…"}))
        if compact_conversation(conv, provider, model):
            task.last_compact_tokens = estimate_tokens(conv)
            on_event(AgentEvent(EventType.LOG, task.id,
                                {"text": f"Compacted. Context now ~{task.last_compact_tokens} tokens."}))
    except Exception:
        pass


# --- confirmation routed to the UI thread -----------------------------------

def request_confirmation(session: Session, task: Task, prompt: str, diff: str,
                         grant_key: str | None = None) -> bool:
    """Called from a worker thread. Auto-approve when accept-edits mode is on, or when
    `grant_key` was 'allowed for this session' (via the confirm's 'a', or config
    `allowed_tools`). Otherwise hand a PendingConfirmation to the UI thread and block
    until answered (a backgrounded task simply waits here until foregrounded)."""
    if session.approve_writes:
        return True
    if grant_key and grant_key in session.granted:
        return True
    pc = PendingConfirmation(prompt=prompt, diff=diff, grant_key=grant_key)
    task.pending = pc
    try:
        while not pc.answered.wait(timeout=0.2):
            if task.cancel_flag.is_set():
                return False
        return pc.approved
    finally:
        task.pending = None


# --- edit safety: detect files changed on disk since 2B read them -----------

def _record_read(task: Task, path: str) -> None:
    """Remember a file's mtime when 2B reads it, so a later edit can tell whether it
    changed on disk in between. Resolves via tools.resolve_read_path so it keys on the
    SAME file do_read_file returned — including a section read (`path:start-end`) or a
    basename fallback where the given path didn't exist verbatim."""
    full = tools.resolve_read_path(path)
    if full and os.path.isfile(full):
        try:
            task.read_mtimes[full] = os.path.getmtime(full)
        except OSError:
            pass


def _stale_check(task: Task, path: str) -> str:
    """Error string if `path` was read earlier and has since changed on disk (edited
    outside 2B), else ''. This check does not force a read-before-write — a file it
    never read is allowed through; it only stops clobbering a file 2B is working from a
    stale copy of. (A separate, narrow gate in apply_write does refuse a full overwrite
    of an existing *unread* file; edit_file stays exempt.) 2B's own writes refresh the
    recorded mtime, so they never trip this.

    Best-effort: mtime-only, so a change that keeps the same mtime (same-second write,
    an editor that restores mtime, a restore from an older backup) or that arrives via
    an unread symlink/case alias won't be caught. It never blocks a legitimate edit —
    the failure mode is a missed detection, not a false positive."""
    full = tools._safe_path(path)
    if not full or full not in task.read_mtimes:
        return ""
    try:
        current = os.path.getmtime(full)
    except OSError:
        return ""
    if current > task.read_mtimes[full]:
        return (f"error: {path} changed on disk since you read it — its current contents differ "
                "from what this edit is based on. read_file it again, then redo the edit.")
    return ""


def _refresh_mtime(task: Task, path: str) -> None:
    """After 2B writes a file, record its new mtime so the next edit isn't falsely
    flagged as stale by our own change."""
    full = tools._safe_path(path)
    if full and os.path.isfile(full):
        try:
            task.read_mtimes[full] = os.path.getmtime(full)
        except OSError:
            pass


# --- file-tool safety: read dedup / read-loop breaker / recovery nudges ------

# Appended to a rejected write/read guard: for cloud models that have run_command,
# stop them "fixing" a refusal with a shell one-liner instead of the file tools.
_NO_SHELL_WORKAROUND = ("Do not work around this with a shell command (sed, awk, a heredoc, "
                        "or echo > file) — use edit_file / write_file.")
READ_LOOP_LIMIT = 4   # consecutive identical unchanged reads before a hard, recoverable stop


def _read_guard(task: Task, path: str) -> str:
    """Short-circuit a wasteful repeated read: return an 'unchanged' stub for an
    identical re-read this turn, or — after READ_LOOP_LIMIT of them with no other
    action in between — a firm, recoverable error (a small model otherwise burns
    the turn budget re-reading the same file). '' means read normally. Keys on the
    exact `path` argument, so a different line-range of the same file is a fresh read.
    The consecutive count is reset by any non-read tool call (see _dispatch_tool)."""
    if path != task.last_read_arg:
        return ""
    full = tools.resolve_read_path(path)
    if not (full and full in task.read_mtimes):
        return ""
    try:
        if os.path.getmtime(full) > task.read_mtimes[full]:   # changed on disk → genuine re-read
            return ""
    except OSError:
        return ""
    # mtime-granularity, same as _stale_check: a change within the same clock tick as
    # the recorded read isn't detected. The window here is tiny (an immediate re-read
    # of the same arg with no action between), so the risk of masking a real change is
    # negligible and not worth a content hash.
    task.read_repeat += 1
    if task.read_repeat >= READ_LOOP_LIMIT:
        # Stable text (no interpolated count): if the model keeps ignoring it, the
        # generic _LoopGuard sees an identical (name, args, result) and hard-stops the
        # task — interpolating the rising count would make every message unique and
        # slip past that net.
        return (f"error: you keep re-reading {path} with no changes and no other action in between — "
                "its contents are already in this conversation. Stop re-reading it: make an edit, run a "
                f"check, or give your final answer. {_NO_SHELL_WORKAROUND}")
    return (f"({path} is unchanged since you read it this turn — its contents are already above; "
            "use them instead of reading it again)")


# --- write/edit wrappers: snapshot for /undo, confirm via UI, then apply -----

def apply_write(session: Session, task: Task, path: str, content: str) -> str:
    stale = _stale_check(task, path)
    if stale:
        return stale
    full = tools._safe_path(path)
    # Read-before-overwrite gate: a full write_file over an EXISTING file 2B hasn't
    # read this session is a blind clobber — it can't see what it's discarding, and
    # in accept-edits/headless mode there's no confirmation to catch it. New files are
    # always allowed; edit_file is exempt (its exact old_text already proves 2B saw the
    # region). 2B's own prior writes refresh read_mtimes, so they don't trip this. (A
    # section read counts as having read the file — a deliberate simplification of this
    # narrow gate; normal mode still shows the full-overwrite confirmation regardless.)
    if full and os.path.isfile(full) and full not in task.read_mtimes:
        return (f"error: write_file would fully overwrite {path}, but you haven't read it this session. "
                "Overwriting an unread file risks discarding content you can't see — read_file it first, "
                f"then write_file; or use edit_file to change only the part you mean. {_NO_SHELL_WORKAROUND}")
    pre = None
    if full and os.path.isfile(full):
        with open(full, "r", errors="replace") as f:
            pre = f.read()
    normalized = content if content.endswith("\n") or not content else content + "\n"
    preview = f"(full overwrite of {path}: {len(normalized.splitlines())} lines)"
    if not request_confirmation(session, task, f"Apply write to {path}?", preview, grant_key="write_file"):
        return "write rejected by user"
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = tools.do_write_file(path, content, auto_yes=True)
    if result.startswith("wrote"):
        task.push_edit(path, pre)
        task.last_diff = preview
        _refresh_mtime(task, path)
        result += diagnostics.summarize(path)
    return result


def apply_edit(session: Session, task: Task, path: str, old_text: str, new_text: str) -> str:
    full = tools._safe_path(path)
    if full is None:
        return "error: empty or invalid path"
    if not os.path.isfile(full):
        return f"error: no such file: {path}"
    stale = _stale_check(task, path)
    if stale:
        return stale
    # newline="" so plan_edit sees the file's real line endings (matches do_edit_file).
    with open(full, "r", errors="replace", newline="") as f:
        pre = f.read()
    status, *rest = tools.plan_edit(pre, old_text, new_text)
    if status == "error":
        return rest[0]
    new_content, _note = rest
    import difflib

    diff = "\n".join(difflib.unified_diff(pre.splitlines(), new_content.splitlines(), lineterm="", n=1))
    if not request_confirmation(session, task, f"Apply edit to {path}?", diff, grant_key="edit_file"):
        return "edit rejected by user"
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = tools.do_edit_file(path, old_text, new_text, auto_yes=True)
    if result.startswith("edited"):
        task.push_edit(path, pre)
        task.last_diff = diff
        _refresh_mtime(task, path)
        result += diagnostics.summarize(path)
    return result


def apply_worker_changes(session: Session, task: Task, changes) -> str:
    """Apply file changes collected from `delegate` workers as a single batch: a
    path touched by more than one worker is a conflict (applied for none, reported);
    non-conflicting paths get one combined confirmation, then are written and
    diagnosed like apply_write. Snapshot is last-applied-wins, matching the
    existing single-level /undo."""
    if not changes:
        return ""
    if task.cancel_flag.is_set():
        return "\n(cancelled — worker changes not applied)"
    if session.read_only:
        return "\n(plan mode — worker changes not applied)"
    by_path: dict[str, list] = {}
    for ap, orig, final, idx in changes:
        by_path.setdefault(ap, []).append((orig, final, idx))
    to_apply, conflicts = [], []
    for ap, group in by_path.items():
        (conflicts if len(group) > 1 else to_apply).append((ap, group))
    # Same edit-safety guard as apply_edit/apply_write: don't let a worker clobber a
    # file the main task read that has since changed on disk.
    stale_paths = [ap for ap, _ in to_apply if _stale_check(task, ap)]
    if stale_paths:
        stale_set = set(stale_paths)
        to_apply = [(ap, group) for ap, group in to_apply if ap not in stale_set]

    import difflib

    previews = []
    currents: dict[str, str] = {}
    for ap, group in to_apply:
        orig, final, _ = group[0]
        cur = orig
        try:
            with open(ap, "r", errors="replace") as f:
                cur = f.read()
        except OSError:
            pass
        currents[ap] = cur
        previews.append("\n".join(difflib.unified_diff(
            cur.splitlines(), final.splitlines(), lineterm="", n=1,
            fromfile=os.path.relpath(ap), tofile=os.path.relpath(ap))))

    lines = []
    if conflicts:
        lines.append("conflict — not applied (multiple workers changed the same file): "
                     + ", ".join(os.path.relpath(ap) for ap, _ in conflicts))
    if stale_paths:
        lines.append("changed on disk since read — not applied (read_file again first): "
                     + ", ".join(os.path.relpath(ap) for ap in stale_paths))
    if not to_apply:
        return "\n" + "\n".join(lines)

    combined_preview = "\n\n".join(previews)
    if not request_confirmation(session, task,
            f"Apply {len(to_apply)} worker change(s)?", combined_preview):
        return "\n" + "\n".join(lines + ["worker changes rejected by user"])

    applied = []
    for ap, group in to_apply:
        _, final, _ = group[0]
        pre = currents[ap]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                res = tools.do_write_file(ap, final, auto_yes=True)
        except OSError as e:
            applied.append(f"error writing {os.path.relpath(ap)}: {e}")
            continue
        if res.startswith("wrote"):
            task.push_edit(ap, pre)
            task.last_diff = combined_preview
            _refresh_mtime(task, ap)
            res += diagnostics.summarize(ap)
        applied.append(res)
    lines.append(f"applied {len(to_apply)} worker change(s):")
    lines.extend(applied)
    return "\n" + "\n".join(lines)


def _run_git(session: Session, task: Task, git_args: str, read_cap: int | None) -> str:
    """Read-only git runs immediately (and in plan mode); mutating git is
    confirmation-gated and refused in plan mode."""
    git_args = (git_args or "").strip()
    if not git_args:
        return "error: no git command given"
    # Reject shell operators up front (do_run_git returns the recoverable error), so a
    # shell-chained mutating command isn't confirm-prompted only to fail on apply.
    if tools.has_shell_syntax(git_args):
        return tools.do_run_git(git_args, max_chars=read_cap)
    if tools.git_is_read_only(git_args):
        return tools.do_run_git(git_args, max_chars=read_cap, cancel=task.cancel_flag)
    if session.read_only:
        return (f"error: plan mode is on — not running mutating git (git {git_args}). Use read-only "
                "git (status/diff/log) to inspect, and present a plan in your final answer.")
    if not request_confirmation(session, task, f"Run: git {git_args}?", f"$ git {git_args}", grant_key="run_git"):
        return "git command rejected by user"
    return tools.do_run_git(git_args, max_chars=read_cap, cancel=task.cancel_flag)


# Required arguments per frozen tool. A small model sometimes emits a tool call with
# args missing (or an empty {}); without this, args["path"] raised KeyError and crashed
# the run with a traceback instead of giving the model a recoverable error. (run_git,
# run_command, list_files read their args with .get and handle emptiness themselves.)
_REQUIRED_ARGS = {
    "read_file": ("path",),
    "search_files": ("query",),
    "edit_file": ("path", "old_text", "new_text"),
    "write_file": ("path", "content"),
}


def _missing_required(name: str, args) -> list[str]:
    """Which required args a tool call is missing — absent OR explicitly null (an
    empty string is allowed: e.g. write_file content=''). All of them if args isn't
    a dict."""
    req = _REQUIRED_ARGS.get(name, ())
    if not req:
        return []
    if not isinstance(args, dict):
        return list(req)
    return [k for k in req if args.get(k) is None]


def _dispatch_tool(session: Session, task: Task, name: str, args: dict, read_cap: int | None = None) -> str:
    if not name:
        # A tool call with no name usually means the model echoed tool-call-like
        # text it read from a file (XML/JSON) as if it were a call. Tell it plainly
        # that quoted markup is data, not something to run — a cheap, high-value
        # guard for an agent that reads a lot of files.
        return ("error: that tool call had no tool name. If you were quoting tool-call-like "
                "text from a file (e.g. XML or JSON you just read), that is data, not a tool "
                "to run — don't emit it as a call. Make a real tool call or give your final answer.")
    missing = _missing_required(name, args)
    if missing:
        need = ", ".join(_REQUIRED_ARGS[name])
        return (f"error: {name} call is missing required argument(s): {', '.join(missing)}. "
                f"Call {name} again with all of: {need}.")
    if name != "read_file":
        # Any non-read action breaks a read streak, so the read-loop breaker only
        # counts *consecutive* identical reads with nothing done in between.
        task.last_read_arg = None
        task.read_repeat = 0
    if session.read_only and name in ("edit_file", "write_file"):
        return ("error: plan mode is on — no changes are applied. Do not call edit_file or "
                "write_file. Investigate with the read-only tools and present your proposed "
                "changes as a concrete, numbered plan in your final answer instead.")
    if name == "run_git":
        return _run_git(session, task, args.get("args", ""), read_cap)
    if name == "run_command":                       # cloud-only shell tool
        if session.read_only:
            return ("error: plan mode is on — not running shell commands. Investigate read-only and "
                    "present a plan in your final answer.")
        cmd = (args.get("command") or "").strip()
        if not cmd:
            return "error: no command given"
        if not request_confirmation(session, task, "Run this shell command?", f"$ {cmd}", grant_key="run_command"):
            return "command rejected by user"
        return tools.do_run_command(cmd, max_chars=read_cap, cancel=task.cancel_flag)
    if mcp_client.manager.is_mcp_tool(name):        # curated MCP tool -> route to its server
        if session.read_only:                       # plan mode: MCP tools may have side effects
            return ("error: plan mode is on — external MCP tools are not run (they may change state). "
                    "Investigate with the read-only tools and present a concrete plan in your final answer.")
        return mcp_client.manager.call_tool(name, args)
    if name == "edit_file":
        return apply_edit(session, task, args["path"], args["old_text"], args["new_text"])
    if name == "write_file":
        return apply_write(session, task, args["path"], args["content"])
    # read-only tools: capture any stray stdout, none expected
    buf = io.StringIO()
    with redirect_stdout(buf):
        if name == "list_files":
            return tools.do_list_files(args.get("path", "."), max_chars=read_cap)
        if name == "read_file":
            guard = _read_guard(task, args["path"])
            if guard:
                return guard
            out = tools.do_read_file(args["path"], max_chars=read_cap)
            _record_read(task, args["path"])
            task.last_read_arg = args["path"]
            task.read_repeat = 1
            return out
        if name == "search_files":
            return tools.do_search_files(args["query"], args.get("path", "."))
    return f"error: unknown tool {name}"


# --- the turn loop -----------------------------------------------------------

def _resolve_subagent_model(reg: dict, provider: Any, model: str) -> tuple[Any, str]:
    """(provider, model) for subagents: TWOB_SUBAGENT_MODEL if set and resolvable, else
    the parent's. Lets you run explorers/workers on a cheaper model than the parent."""
    name = os.environ.get("TWOB_SUBAGENT_MODEL")
    if name:
        r = registry.resolve(reg, name)
        if r is not None:
            return r
    return provider, model


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


def run_task(session: Session, task: Task, on_event: Callable[[AgentEvent], None],
             reg: dict | None = None) -> None:
    """Drive the tool-call loop for one task on the calling (worker) thread.
    Resolves the active model to a provider, then drives it via the canonical
    Conversation. Emits events for the UI thread; never writes the terminal."""
    _trace_path = os.environ.get("TWOB_TRACE")
    if _trace_path:
        on_event = _traced(on_event, _trace_path)
    reg = reg if reg is not None else registry.build_registry()
    model_str = task.model_override or session.default_model
    resolved = registry.resolve(reg, model_str)
    if resolved is None:
        _finish_failed(task, on_event,
                       f"could not resolve model '{model_str}' to a configured provider (try /models)")
        return
    provider, model = resolved
    # A single read/listing may use ~55% of the model's token budget (≈ tokens*2.2
    # chars). Small local windows get a section suggestion for bigger files; large
    # cloud windows are effectively unbounded.
    read_cap = int(context_budget(provider, model) * 4 * 0.55)
    # Local models get the constrained git-only tool; cloud (frontier) models get
    # the full shell tool. See toolspec.specs_for / _dispatch_tool.
    is_local = getattr(provider, "name", "") == "ollama" and getattr(provider, "api_key", None) is None
    sub_provider, sub_model = _resolve_subagent_model(reg, provider, model)

    if task.conversation is None:
        doc = _project_context()
        sysprompt = SYSTEM_PROMPT + (f"\n\n# Project map (from /init)\n{doc}" if doc else "")
        task.conversation = Conversation(system_prompt=sysprompt)
    conv = task.conversation
    desc = task.description
    if session.read_only:
        desc += ("\n\n(Plan mode is on: do NOT edit or write files. Use the read-only tools to "
                 "investigate, then present a concrete, numbered plan as your final answer.)")
    conv.append(Message.user(desc))

    first_turn = not any(m.role.value == "assistant" for m in conv.messages)
    loop_guard = _LoopGuard()
    promise_nudges = 0   # times we've nudged a "said it'd call a tool but didn't" turn
    try:
        # Valid tool names for this task, so coerce_tool_args can let a name nested in
        # a malformed wrapper override an empty/unknown outer name. Fixed for the task;
        # inside the try so even a surprise here lands on the never-throw closure.
        known_tools = tuple(s.name for s in _active_specs(is_local))
        for _ in range(MAX_TURNS):
            if task.cancel_flag.is_set():
                _finish_stopped(task, on_event)
                return

            _maybe_compact(conv, provider, model, task, on_event)
            task.status_line = "Thinking"
            task.turn_started_at = time.monotonic()
            # Best-effort perf readout (local models). May be blank on the very
            # first turn until the model finishes loading; refreshed on the first
            # streamed token below, and persists across turns once set.
            if getattr(provider, "name", "") == "ollama" and hasattr(provider, "perf"):
                try:
                    p = provider.perf(model)
                    if p:
                        task.perf = p
                except Exception:
                    pass
            on_event(AgentEvent(EventType.TURN_START, task.id))

            streamed = {"n": 0, "perf": False}

            def on_text(chunk: str, _t=task, _p=provider, _m=model) -> None:
                if _t.cancel_flag.is_set():          # esc pressed mid-stream -> abort now
                    raise _Interrupted()
                if not streamed["perf"] and getattr(_p, "name", "") == "ollama" and hasattr(_p, "perf"):
                    streamed["perf"] = True
                    try:
                        val = _p.perf(_m)
                        if val:
                            _t.perf = val
                    except Exception:
                        pass
                streamed["n"] += len(chunk)
                on_event(AgentEvent(EventType.ASSISTANT_DELTA, _t.id, {"chunk": chunk}))

            active_specs = _active_specs(is_local)
            req_conv = conv if os.environ.get("TWOB_NO_TRIM") else conversation.trimmed(conv)
            try:
                resp = stream_with_retry(provider, req_conv, model, active_specs, on_text, cancel=task.cancel_flag)
            except _Interrupted:
                _finish_stopped(task, on_event)
                return
            except Exception as e:
                _finish_failed(task, on_event, _classify_exc(e))
                return

            msg = resp.message
            content = (msg.text or "").strip()

            if first_turn and content:
                parsed = planparse.extract_plan(content)
                if parsed:
                    task.plan_steps = parsed
            first_turn = False

            if not msg.tool_calls:
                # Caught the model narrating a tool call it never made — give it another
                # turn to actually do it (bounded, so a model that just keeps talking
                # still finalizes rather than looping).
                if promise_nudges < 2 and _promised_tool_but_didnt(content):
                    promise_nudges += 1
                    conv.append(msg)
                    conv.append(Message.user(_PROMISE_NUDGE))
                    continue
                planparse.finalize_steps(task.plan_steps)
                task.status_line = ""
                task.state = TaskState.DONE
                # If nothing streamed (e.g. answer landed in `thinking`), emit it now.
                if streamed["n"] == 0:
                    fallback = content or (msg.thinking or "").strip()
                    if not fallback:
                        # No content and no call. Name the cause instead of re-prompting
                        # the same wall: a length/truncation stop is a distinct, reportable
                        # condition, not a genuine empty answer.
                        fallback = ("(model output was cut off at its length limit)"
                                    if resp.done_reason == "length"
                                    else "(model returned an empty response)")
                    on_event(AgentEvent(EventType.ASSISTANT_DELTA, task.id, {"chunk": fallback}))
                on_event(AgentEvent(EventType.TASK_DONE, task.id))
                return

            conv.append(msg)
            results = []
            nudge_pending = False
            for tc in msg.tool_calls:
                if task.cancel_flag.is_set():        # esc while tools are running
                    _finish_stopped(task, on_event)
                    return
                # Normalize a malformed-but-recoverable call shape (stringified args,
                # args nested under an "arguments" key, name only inside the wrapper)
                # before anything reads it — so display, plan inference, dispatch, and
                # the loop-guard all see the same coerced values that actually ran.
                tc.name, tc.arguments = tools.coerce_tool_args(tc.name, tc.arguments, known_tools)
                planparse.infer_active_step(task.plan_steps, tc.name, tc.arguments)
                task.status_line = _STATUS.get(tc.name, "Working")
                shown = {k: (v if k != "content" else f"<{len(v)} chars>") for k, v in tc.arguments.items()}
                on_event(AgentEvent(EventType.TOOL_CALL_START, task.id, {"name": tc.name, "shown": shown}))
                try:
                    result = _dispatch_tool(session, task, tc.name, tc.arguments, read_cap)
                except Exception as e:
                    # esc can tear a tool's helper (LSP/MCP) out from under it mid-call;
                    # when cancelled, finish quietly rather than surfacing that as an error.
                    # Log the exception first so an *unrelated* failure that merely
                    # coincided with the stop isn't lost without a trace.
                    if task.cancel_flag.is_set():
                        on_event(AgentEvent(EventType.LOG, task.id,
                                            {"text": f"(stopped while {tc.name} was running: {e})"}))
                        _finish_stopped(task, on_event)
                        return
                    raise
                on_event(AgentEvent(EventType.TOOL_CALL_RESULT, task.id, {"name": tc.name, "result": result}))
                results.append(ToolResult(tool_call_id=tc.id, content=result))
                verdict = loop_guard.record(tc.name, tc.arguments, result)
                if verdict == "stop":
                    conv.append(Message.results(results))
                    task.status_line = ""
                    on_event(AgentEvent(EventType.LOG, task.id,
                                        {"text": f"Stopped: {tc.name} repeated with no progress."}))
                    _finish_stopped(task, on_event)
                    return
                if verdict == "nudge":
                    nudge_pending = True
            conv.append(Message.results(results))
            if nudge_pending:
                conv.append(Message.user(_LOOP_NUDGE))

        # Tool budget exhausted — one final turn for a best-effort answer (no more
        # tools), so the user gets a summary instead of a bare error.
        conv.append(Message.user(
            "You've reached the tool-call limit. Give your best final answer now, "
            "based on what you've already found — do not call any more tools."))
        _maybe_compact(conv, provider, model, task, on_event)
        task.status_line = "Thinking"
        task.turn_started_at = time.monotonic()
        on_event(AgentEvent(EventType.TURN_START, task.id))
        got = {"n": 0}

        def on_final(chunk: str, _t=task) -> None:
            if _t.cancel_flag.is_set():
                raise _Interrupted()
            got["n"] += len(chunk)
            on_event(AgentEvent(EventType.ASSISTANT_DELTA, _t.id, {"chunk": chunk}))

        req_conv = conv if os.environ.get("TWOB_NO_TRIM") else conversation.trimmed(conv)
        try:
            resp = stream_with_retry(provider, req_conv, model, _active_specs(is_local), on_final, cancel=task.cancel_flag)
            planparse.finalize_steps(task.plan_steps)
            task.status_line = ""
            task.state = TaskState.DONE
            if got["n"] == 0:
                txt = (resp.message.text or resp.message.thinking or "").strip()
                if not txt:
                    txt = ("(model output was cut off at its length limit)"
                           if resp.done_reason == "length"
                           else "(reached the tool-call limit without a final answer)")
                on_event(AgentEvent(EventType.ASSISTANT_DELTA, task.id, {"chunk": txt}))
            on_event(AgentEvent(EventType.TASK_DONE, task.id))
        except _Interrupted:
            _finish_stopped(task, on_event)
        except Exception as e:
            _finish_failed(task, on_event, f"max turns reached; final attempt failed: {_classify_exc(e)}")
    except _Interrupted:
        # Net for any interrupt that escaped an inner handler — finish quietly, not red.
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
            from . import persist
            persist.save(task.id, session.cwd, task.title,
                         task.model_override or session.default_model, task.conversation)
        except Exception:
            pass
