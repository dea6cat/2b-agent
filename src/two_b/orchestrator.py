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
import io
import os
import time
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from . import diagnostics, mcp_client, planparse, registry, tools
from .conversation import Conversation, Message, Role, ToolResult
from .providers.base import ProviderError
from .session import PendingConfirmation, Session, Task, TaskState
from .toolspec import TOOL_SPECS, specs_for

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
SYSTEM_PROMPT = BASE_SYSTEM_PROMPT + planparse.PLAN_PROMPT

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
    get a small MCP cap so a big enabled set can't flood their tool list."""
    mcp = mcp_client.manager.tool_specs()
    if is_local:
        mcp = mcp[:MCP_LOCAL_CAP]
    return specs_for(is_local) + mcp


def context_budget(provider, model: str) -> int:
    """Token budget for a provider/model. For local Ollama this is the window 2B
    actually pins via num_ctx — detected from the model (min of its trained max and
    a RAM-safe cap), or TWOB_CONTEXT_TOKENS if set — so the budget matches reality."""
    name = getattr(provider, "name", "")
    if name == "ollama" and hasattr(provider, "context_window"):
        try:
            return provider.context_window(model)
        except Exception:
            pass
    env = os.environ.get("TWOB_CONTEXT_TOKENS")
    if name == "ollama" and env and env.isdigit():
        return int(env)
    return CONTEXT_BUDGETS.get(name, 8000)


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

def request_confirmation(session: Session, task: Task, prompt: str, diff: str) -> bool:
    """Called from a worker thread. If auto-approve is on, approve immediately.
    Otherwise hand a PendingConfirmation to the UI thread and block until it is
    answered (a backgrounded task simply waits here until foregrounded)."""
    if session.approve_writes:
        return True
    pc = PendingConfirmation(prompt=prompt, diff=diff)
    task.pending = pc
    try:
        while not pc.answered.wait(timeout=0.2):
            if task.cancel_flag.is_set():
                return False
        return pc.approved
    finally:
        task.pending = None


# --- write/edit wrappers: snapshot for /undo, confirm via UI, then apply -----

def apply_write(session: Session, task: Task, path: str, content: str) -> str:
    full = tools._safe_path(path)
    pre = None
    if full and os.path.isfile(full):
        with open(full, "r", errors="replace") as f:
            pre = f.read()
    normalized = content if content.endswith("\n") or not content else content + "\n"
    preview = f"(full overwrite of {path}: {len(normalized.splitlines())} lines)"
    if not request_confirmation(session, task, f"Apply write to {path}?", preview):
        return "write rejected by user"
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = tools.do_write_file(path, content, auto_yes=True)
    if result.startswith("wrote"):
        task.last_edit_snapshot = (path, pre)
        task.last_diff = preview
        result += diagnostics.summarize(path)
    return result


def apply_edit(session: Session, task: Task, path: str, old_text: str, new_text: str) -> str:
    full = tools._safe_path(path)
    if full is None:
        return "error: empty or invalid path"
    if not os.path.isfile(full):
        return f"error: no such file: {path}"
    with open(full, "r", errors="replace") as f:
        pre = f.read()
    status, *rest = tools.plan_edit(pre, old_text, new_text)
    if status == "error":
        return rest[0]
    new_content, _note = rest
    import difflib

    diff = "\n".join(difflib.unified_diff(pre.splitlines(), new_content.splitlines(), lineterm="", n=1))
    if not request_confirmation(session, task, f"Apply edit to {path}?", diff):
        return "edit rejected by user"
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = tools.do_edit_file(path, old_text, new_text, auto_yes=True)
    if result.startswith("edited"):
        task.last_edit_snapshot = (path, pre)
        task.last_diff = diff
        result += diagnostics.summarize(path)
    return result


def _run_git(session: Session, task: Task, git_args: str, read_cap: int | None) -> str:
    """Read-only git runs immediately (and in plan mode); mutating git is
    confirmation-gated and refused in plan mode."""
    git_args = (git_args or "").strip()
    if not git_args:
        return "error: no git command given"
    if tools.git_is_read_only(git_args):
        return tools.do_run_git(git_args, max_chars=read_cap)
    if session.read_only:
        return (f"error: plan mode is on — not running mutating git (git {git_args}). Use read-only "
                "git (status/diff/log) to inspect, and present a plan in your final answer.")
    if not request_confirmation(session, task, f"Run: git {git_args}?", f"$ git {git_args}"):
        return "git command rejected by user"
    return tools.do_run_git(git_args, max_chars=read_cap)


def _dispatch_tool(session: Session, task: Task, name: str, args: dict, read_cap: int | None = None) -> str:
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
        if not request_confirmation(session, task, "Run this shell command?", f"$ {cmd}"):
            return "command rejected by user"
        return tools.do_run_command(cmd, max_chars=read_cap)
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
            return tools.do_read_file(args["path"], max_chars=read_cap)
        if name == "search_files":
            return tools.do_search_files(args["query"], args.get("path", "."))
    return f"error: unknown tool {name}"


# --- the turn loop -----------------------------------------------------------

def run_task(session: Session, task: Task, on_event: Callable[[AgentEvent], None],
             reg: dict | None = None) -> None:
    """Drive the tool-call loop for one task on the calling (worker) thread.
    Resolves the active model to a provider, then drives it via the canonical
    Conversation. Emits events for the UI thread; never writes the terminal."""
    reg = reg if reg is not None else registry.build_registry()
    model_str = task.model_override or session.default_model
    resolved = registry.resolve(reg, model_str)
    if resolved is None:
        err = f"could not resolve model '{model_str}' to a configured provider (try /models)"
        task.state = TaskState.ERROR
        task.error = err
        on_event(AgentEvent(EventType.TASK_ERROR, task.id, {"error": err}))
        return
    provider, model = resolved
    # A single read/listing may use ~55% of the model's token budget (≈ tokens*2.2
    # chars). Small local windows get a section suggestion for bigger files; large
    # cloud windows are effectively unbounded.
    read_cap = int(context_budget(provider, model) * 4 * 0.55)
    # Local models get the constrained git-only tool; cloud (frontier) models get
    # the full shell tool. See toolspec.specs_for / _dispatch_tool.
    is_local = getattr(provider, "name", "") == "ollama" and getattr(provider, "api_key", None) is None

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
    try:
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
            try:
                resp = provider.stream(conv, model, active_specs, on_text)
            except _Interrupted:
                _finish_stopped(task, on_event)
                return
            except ProviderError as e:
                task.state = TaskState.ERROR
                task.error = str(e)
                on_event(AgentEvent(EventType.TASK_ERROR, task.id, {"error": str(e)}))
                return
            except Exception as e:
                task.state = TaskState.ERROR
                task.error = str(e)
                on_event(AgentEvent(EventType.TASK_ERROR, task.id, {"error": str(e)}))
                return

            msg = resp.message
            content = (msg.text or "").strip()

            if first_turn and content:
                parsed = planparse.extract_plan(content)
                if parsed:
                    task.plan_steps = parsed
            first_turn = False

            if not msg.tool_calls:
                planparse.finalize_steps(task.plan_steps)
                task.status_line = ""
                task.state = TaskState.DONE
                # If nothing streamed (e.g. answer landed in `thinking`), emit it now.
                if streamed["n"] == 0:
                    fallback = content or (msg.thinking or "").strip() or "(model returned an empty response)"
                    on_event(AgentEvent(EventType.ASSISTANT_DELTA, task.id, {"chunk": fallback}))
                on_event(AgentEvent(EventType.TASK_DONE, task.id))
                return

            conv.append(msg)
            results = []
            for tc in msg.tool_calls:
                if task.cancel_flag.is_set():        # esc while tools are running
                    _finish_stopped(task, on_event)
                    return
                planparse.infer_active_step(task.plan_steps, tc.name, tc.arguments)
                task.status_line = _STATUS.get(tc.name, "Working")
                shown = {k: (v if k != "content" else f"<{len(v)} chars>") for k, v in tc.arguments.items()}
                on_event(AgentEvent(EventType.TOOL_CALL_START, task.id, {"name": tc.name, "shown": shown}))
                result = _dispatch_tool(session, task, tc.name, tc.arguments, read_cap)
                on_event(AgentEvent(EventType.TOOL_CALL_RESULT, task.id, {"name": tc.name, "result": result}))
                results.append(ToolResult(tool_call_id=tc.id, content=result))
            conv.append(Message.results(results))

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

        try:
            resp = provider.stream(conv, model, _active_specs(is_local), on_final)
            planparse.finalize_steps(task.plan_steps)
            task.status_line = ""
            task.state = TaskState.DONE
            if got["n"] == 0:
                txt = (resp.message.text or resp.message.thinking or "").strip() or \
                    "(reached the tool-call limit without a final answer)"
                on_event(AgentEvent(EventType.ASSISTANT_DELTA, task.id, {"chunk": txt}))
            on_event(AgentEvent(EventType.TASK_DONE, task.id))
        except _Interrupted:
            _finish_stopped(task, on_event)
        except Exception as e:
            task.state = TaskState.ERROR
            task.error = f"max turns reached; final attempt failed: {e}"
            on_event(AgentEvent(EventType.TASK_ERROR, task.id, {"error": task.error}))
    finally:
        if task.status_line:
            task.status_line = ""
