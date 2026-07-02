"""Cloud-path subagents: parallel, isolated read-only explorers behind the `delegate`
tool. Each runs in its own Conversation with only the read tools and returns a distilled
findings report — heavy file reading happens here and never enters the parent context."""
from __future__ import annotations
import concurrent.futures
import os
import threading
from . import tools
from .conversation import Conversation, Message, ToolResult

EXPLORER_PROMPT = (
    "You are a read-only exploration agent. Investigate the goal using list_files, "
    "read_file, and search_files, then STOP and reply with a concise findings report: "
    "what you found, the concrete file:line references, and anything the caller needs. "
    "You cannot edit, write, or run commands. Keep the report short — it is folded back "
    "into another agent's context, so summarize; do not paste large file bodies."
)

def _read_dispatch(name: str, args: dict, read_cap: int | None) -> str:
    if name == "list_files":
        return tools.do_list_files(args.get("path", "."), max_chars=read_cap)
    if name == "read_file":
        return tools.do_read_file(args["path"], max_chars=read_cap)
    if name == "search_files":
        return tools.do_search_files(args["query"], args.get("path", "."))
    return f"error: '{name}' is not available to an explorer (read-only)"


def run_explorer(goal, provider, model, read_cap=None, max_turns=8, cancel=None):
    conv = Conversation(system_prompt=EXPLORER_PROMPT)
    conv.append(Message.user(goal))
    specs = tuple(s for s in _explorer_specs())          # read-only tool specs
    for _ in range(max_turns):
        if cancel is not None and cancel.is_set():
            return "explorer cancelled"
        resp = provider.stream(conv, model, specs, lambda _c: None)
        msg = resp.message
        conv.append(msg)
        if not msg.tool_calls:
            return (msg.text or "").strip() or "(explorer produced no findings)"
        results = [ToolResult(tool_call_id=tc.id,
                              content=_read_dispatch(tc.name, tc.arguments, read_cap))
                   for tc in msg.tool_calls]
        conv.append(Message.results(results))
    return "(explorer hit its turn limit without a final report)"


def _explorer_specs():
    from .toolspec import TOOL_SPECS
    keep = {"list_files", "read_file", "search_files"}
    return [s for s in TOOL_SPECS if s.name in keep]


class _AnyEvent:
    """Read-only OR of several threading.Events: is_set() is True if any is set.
    Lets an explorer honor both the parent task's cancel (esc) and delegate's own
    batch-timeout signal, while delegate only ever sets its OWN event."""
    def __init__(self, *events):
        self._events = [e for e in events if e is not None]
    def is_set(self) -> bool:
        return any(e.is_set() for e in self._events)


MAX_PARALLEL = 4
DELEGATE_TIMEOUT = 180  # seconds, wall-clock budget for the whole batch
_MAX_SECTION = 4000


def delegate(tasks, provider, model, read_cap=None, on_event=None, cancel=None) -> str:
    tasks = [t for t in (tasks or []) if isinstance(t, dict) and t.get("goal")]
    if not tasks:
        return "error: delegate needs at least one {role, goal} task"

    sub_cancel = threading.Event()
    combined = _AnyEvent(cancel, sub_cancel)

    def _one(t):
        role, goal = (t.get("role") or "explore"), t["goal"]
        if role == "work":
            return role, goal, "(worker delegation is not enabled yet — Phase 2)"
        try:
            return role, goal, run_explorer(goal, provider, model, read_cap=read_cap, cancel=combined)
        except Exception as e:  # a subagent failing must not kill the batch
            return role, goal, f"(explorer error: {str(e)[:200]})"

    # Not a `with` block on purpose: ThreadPoolExecutor.__exit__ calls
    # shutdown(wait=True), which would block on any straggler exactly like the
    # timeout below is meant to avoid. We call shutdown() exactly once, with
    # wait=False, so this function returns as soon as the batch timeout hits.
    results: list[tuple[str, str, str] | None] = [None] * len(tasks)
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL)
    futures = {ex.submit(_one, t): i for i, t in enumerate(tasks)}
    try:
        for fut in concurrent.futures.as_completed(futures, timeout=DELEGATE_TIMEOUT):
            results[futures[fut]] = fut.result()
    except concurrent.futures.TimeoutError:
        sub_cancel.set()
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    lines = [f"## delegate results ({len(results)} task(s))"]
    for i, (t, r) in enumerate(zip(tasks, results), 1):
        if r is None:
            role, goal = (t.get("role") or "explore"), t["goal"]
            out = "(timed out)"
        else:
            role, goal, out = r
            if len(out) > _MAX_SECTION:
                out = out[:_MAX_SECTION] + " …[truncated]"
        lines.append(f"\n### [{i}] {role}: {goal}\n{out}")
    return "\n".join(lines)


class _WorkerFS:
    """In-memory file state for a worker: reads see the worker's own pending edits,
    edits validate against that virtual content via tools.plan_edit but never write to
    disk. `changes()` yields the final desired content per touched file."""
    def __init__(self):
        self._pending: dict[str, str] = {}    # abspath -> current virtual content
        self._orig: dict[str, str] = {}       # abspath -> content first seen on disk

    def _load(self, path: str) -> str:
        ap = os.path.abspath(path)
        if ap not in self._orig:
            try:
                with open(ap, "r", errors="replace") as f:
                    disk = f.read()
            except OSError:
                disk = ""
            self._orig[ap] = disk
            self._pending.setdefault(ap, disk)
        return ap

    def read(self, path: str) -> str:
        ap = self._load(path)
        return self._pending[ap]

    def edit(self, path: str, old_text: str, new_text: str) -> str:
        ap = self._load(path)
        status, *rest = tools.plan_edit(self._pending[ap], old_text, new_text)
        if status == "error":
            return rest[0]
        self._pending[ap] = rest[0]
        return f"recorded edit to {os.path.relpath(ap)}{rest[1]}"

    def write(self, path: str, content: str) -> str:
        ap = self._load(path)
        self._pending[ap] = content if (content.endswith("\n") or not content) else content + "\n"
        return f"recorded write to {os.path.relpath(ap)}"

    def changes(self):
        return [(ap, self._orig[ap], self._pending[ap])
                for ap in self._pending if self._pending[ap] != self._orig[ap]]
