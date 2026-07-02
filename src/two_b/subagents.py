"""Cloud-path subagents: parallel, isolated read-only explorers behind the `delegate`
tool. Each runs in its own Conversation with only the read tools and returns a distilled
findings report — heavy file reading happens here and never enters the parent context."""
from __future__ import annotations
import concurrent.futures
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
