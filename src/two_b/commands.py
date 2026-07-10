"""Slash-command dispatch. Any input starting with '/' is handled here on the
UI thread and never reaches the model. Handlers operate on the App controller
(duck-typed: .session, .console, and task-control methods) to avoid a circular
import with cli.py.
"""
import json
import os
import re
import shlex
import time

from . import config, difffmt, mcp_client, orchestrator, registry, repomap, tools, untrusted, web
from .conversation import Conversation, Message
from .session import MODE_ACCEPT, MODE_NORMAL, MODE_PLAN, MODE_LABELS, TaskState

COMMANDS = {}

# Tools that /tool can invoke directly (bypassing the model). The frozen five plus the two
# exec tools; MCP and delegate are excluded (they're not part of the frozen schema).
DIRECT_TOOLS = ("list_files", "read_file", "search_files", "edit_file", "write_file",
                "run_git", "run_command")
_MUTATING_TOOLS = ("edit_file", "write_file")
_DELETE_CMDS = {"rm", "rmdir", "unlink", "shred", "dd", "truncate"}


def _is_delete_command(cmd: str) -> bool:
    """Whether a shell/git command's intent is deletion — anchored on the FIRST token of each
    &&/||/;/| segment (the actual command being run), so 'echo rm …' isn't misread as a delete.
    Best-effort display hint only; cmdguard is the real gate."""
    for seg in re.split(r"[;&|]+", cmd):
        toks = seg.split()
        if not toks:
            continue
        head = toks[0]
        if head in _DELETE_CMDS:
            return True
        if head == "find" and "-delete" in toks:
            return True
        if head in ("git", "branch"):                        # 'git rm', 'git clean -fd', 'branch -D', 'git branch -D'
            if "rm" in toks or "clean" in toks:
                return True
            if "branch" in toks and ("-D" in toks or "--delete" in toks):
                return True
    return False


def parse_tool_invocation(rest: str):
    """Parse '/tool <name> key=val …' or '/tool <name> {json}' into (name, args_dict).
    Returns (None, error_message) on any problem. Pure and testable — no side effects."""
    rest = (rest or "").strip()
    if not rest:
        return None, "Usage: /tool <name> key=val …   or   /tool <name> {\"key\": \"val\"}"
    parts = rest.split(maxsplit=1)
    name = parts[0]
    argstr = parts[1].strip() if len(parts) > 1 else ""
    if name not in DIRECT_TOOLS:
        return None, f"'{name}' is not a directly-invocable tool. One of: {', '.join(DIRECT_TOOLS)}."
    if not argstr:
        return name, {}
    if argstr.startswith("{"):
        try:
            args = json.loads(argstr)
        except ValueError as e:
            return None, f"invalid JSON args: {e}"
        if not isinstance(args, dict):
            return None, "JSON args must be an object, e.g. {\"path\": \"a.dart\"}."
        return name, args
    try:
        tokens = shlex.split(argstr)
    except ValueError as e:
        return None, f"could not parse args ({e}). Quote values with spaces, or pass JSON."
    args: dict = {}
    for tok in tokens:
        if "=" not in tok:
            return None, f"expected key=value, got '{tok}'. Use key=val pairs or a {{json}} object."
        k, v = tok.split("=", 1)
        args[k] = v
    return name, args


def confirmation_risk(grant_key: str | None, diff: str) -> tuple[str, str]:
    """A (risk_class, one-line-impact) label for an inline confirmation (P19). Pure. risk_class
    is one of write / execute / delete / change; impact is a short human summary."""
    diff = diff or ""
    if grant_key in _MUTATING_TOOLS:
        if difffmt.is_unified_diff(diff):
            adds = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
            dels = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
            return "write", f"+{adds}/-{dels} lines"
        first = diff.strip().splitlines()[0] if diff.strip() else "file contents"
        return "write", first[:80]
    if grant_key in ("run_command", "run_git"):
        cmd = diff.lstrip("$ ").strip().splitlines()[0] if diff.strip() else ""
        risk = "delete" if _is_delete_command(cmd) else "execute"
        return risk, cmd[:80]
    return "change", ""


def command(*names):
    def deco(fn):
        for n in names:
            COMMANDS[n] = fn
        return fn
    return deco


def command_specs() -> list[tuple[str, str]]:
    """Ordered (primary_name, one-line-doc) for each command, deduped by handler
    (aliases collapse to the first-registered name). Drives the / completion menu."""
    specs: list[tuple[str, str]] = []
    seen = set()
    for name, fn in COMMANDS.items():
        if fn in seen:
            continue
        seen.add(fn)
        doc = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
        specs.append((name, doc))
    return specs


def dispatch_input(raw: str, app) -> bool:
    """Return True if the line was a handled slash command (never reaches the
    model); False if it should be treated as task input."""
    if not raw.startswith("/"):
        return False
    parts = raw[1:].split(maxsplit=1)
    if not parts:
        return False
    name, rest = parts[0], (parts[1] if len(parts) > 1 else "")
    handler = COMMANDS.get(name)
    if handler is None:
        app.ui.print(f"[red]Unknown command:[/red] /{name}  (try /help)")
        return True
    handler(rest, app)
    return True


def _target_task(app):
    """The task a state command acts on: the active one, else the most recent."""
    return app.session.active_task or (app.session.tasks[-1] if app.session.tasks else None)


@command("help", "h")
def _help(rest, app):
    """Show this help."""
    app.ui.print("[bold]Commands:[/bold]")
    seen = set()
    for name, fn in COMMANDS.items():
        if fn in seen:
            continue
        seen.add(fn)
        doc = (fn.__doc__ or "").strip().splitlines()[0]
        app.ui.print(f"  [cyan]/{name}[/cyan] — {doc}")
    app.ui.print("  Type anything else to run it as a task.")


@command("model")
def _model(rest, app):
    """Show or switch the model. Bare name or provider:name; context is preserved."""
    if not rest.strip():
        app.ui.print(f"Current model: [bold]{app.session.default_model}[/bold]")
        return
    name = rest.strip()
    resolved = registry.resolve(app.registry, name)
    if resolved is None:
        app.ui.print(
            f"[red]Could not resolve model:[/red] {name}. "
            "It may be ambiguous — try [bold]provider:model[/bold]. See /models."
        )
        return
    provider, model = resolved
    # Context is preserved: the active task keeps its canonical Conversation and
    # simply gets re-serialized for the new provider on the next turn. We store
    # the fully-qualified 'provider:model' so resolution is unambiguous later.
    app.session.default_model = f"{provider.name}:{model}"
    active = app.session.active_task
    if active is not None:
        active.model_override = f"{provider.name}:{model}"
    _model_changed(app)
    app.ui.print(f"Model set to [bold]{provider.name}:{model}[/bold] (context preserved).")


def _model_changed(app) -> None:
    """Let the UI refresh anything model-specific (e.g. the context-window meter/label)
    after a model switch. No-op on apps that don't implement the hook."""
    hook = getattr(app, "on_model_changed", None)
    if callable(hook):
        hook()


def _model_label(app, qualified: str) -> str:
    """' (local)' / ' (cloud)' for a 'provider:model' string, by its provider prefix
    (independent of whether that provider is currently reachable). '' if unknown."""
    prefix = qualified.split(":", 1)[0] if ":" in qualified else ""
    prov = app.registry.get(prefix)
    if prov is None:
        return ""
    return " (local)" if registry.is_local(prov) else " (cloud)"


@command("default")
def _default(rest, app):
    """Show or set the persisted default model (local or cloud)."""
    saved = config.get_prefs().get("default_model")
    if not rest.strip():
        if saved:
            app.ui.print(f"Default model: [bold]{saved}[/bold]{_model_label(app, saved)}")
        else:
            app.ui.print("No default model saved — set one with [bold]/default <model>[/bold].")
        active = app.session.default_model
        if active and active != saved:
            app.ui.print(f"Active this session: [bold]{active}[/bold]{_model_label(app, active)}")
        return
    name = rest.strip()
    resolved = registry.resolve(app.registry, name)
    if resolved is None:
        app.ui.print(
            f"[red]Could not resolve model:[/red] {name}. "
            "It may be ambiguous — try [bold]provider:model[/bold]. See /models."
        )
        return
    provider, model = resolved
    qualified = f"{provider.name}:{model}"
    # Switch it in for this session (context preserved, exactly like /model)…
    app.session.default_model = qualified
    active = app.session.active_task
    if active is not None:
        active.model_override = qualified
    # …and remember it as the startup default for future sessions.
    config.set_pref("default_model", qualified)
    _model_changed(app)
    kind = "local" if registry.is_local(provider) else "cloud"
    app.ui.print(f"Default set to [bold]{qualified}[/bold] ({kind}), context preserved.")


def _show_connections(app):
    saved = config.saved_providers()
    app.ui.print("[bold]Providers:[/bold]")
    for p in config.PROVIDER_KEY_ENV:
        label = "ollama (cloud)" if p == "ollama" else p
        mark = "[green]connected[/green]" if config.is_connected(p) else "[dim]not connected[/dim]"
        src = "  [dim](saved in 2B)[/dim]" if p in saved else ""
        app.ui.print(f"  {label:<16} {mark}{src}")
    app.ui.print("  [dim]/connect <provider> to add a key · /disconnect <provider> to remove.[/dim]")
    app.ui.print("  [dim]Local Ollama needs no key; a key only enables Ollama Cloud.[/dim]")


@command("connect", "login")
def _connect(rest, app):
    """Connect a provider by saving its API key: /connect <provider> [key]."""
    parts = rest.split(maxsplit=1)
    if not parts:
        _show_connections(app)
        return
    provider = parts[0].lower()
    if provider not in config.PROVIDER_KEY_ENV:
        app.ui.print(f"[red]Unknown provider '{provider}'.[/red] Known: {', '.join(config.PROVIDER_KEY_ENV)}")
        return
    if len(parts) == 2 and parts[1].strip():             # key given inline
        _apply_connect(app, provider, parts[1].strip())
        return
    if hasattr(app, "begin_connect"):                    # TUI: collect it in a masked modal
        app.begin_connect(provider)
    else:
        app.ui.print(f"Usage: /connect {provider} <api-key>")


def _apply_connect(app, provider, key):
    config.connect(provider, key)
    app.registry = registry.build_registry()             # re-detect with the new key
    app.ui.print(f"Connected [bold]{provider}[/bold] ([dim]{config.mask(key)}[/dim]). Saved for future sessions.")


@command("mcp")
def _mcp(rest, app):
    """Manage MCP servers/tools: /mcp · /mcp tools <server> · /mcp enable|disable <server> [tool…|all]."""
    parts = rest.split()
    if not parts:
        _mcp_status(app)
        return
    sub, args = parts[0].lower(), parts[1:]
    if sub == "tools" and args:
        _mcp_list_tools(app, args[0])
    elif sub in ("enable", "disable") and args:
        _mcp_toggle(app, sub, args[0], args[1:])
    else:
        app.ui.print("Usage: /mcp · /mcp tools <server> · /mcp enable <server> <tool…|all> · "
                     "/mcp disable <server> [tool…|all]")


def _mcp_status(app):
    servers = mcp_client.load_servers()
    if not servers:
        app.ui.print("No MCP servers configured. Add them under [bold]\"mcpServers\"[/bold] in "
                     "[bold]~/.config/2b/mcp.json[/bold] or ./.mcp.json.")
        return
    enabled = mcp_client.load_enabled()
    app.ui.print("[bold]MCP servers:[/bold]")
    for name in servers:
        en = enabled.get(name, [])
        err = mcp_client.manager.error(name)
        state = f"[green]{len(en)} tool(s) enabled[/green]" if en else "[dim]no tools enabled[/dim]"
        note = f"  [red]({err})[/red]" if err else ""
        app.ui.print(f"  {name:<16} {state}{note}")
    app.ui.print("  [dim]/mcp tools <server> to list · /mcp enable <server> <tool…|all> to expose[/dim]")


def _mcp_list_tools(app, server):
    if server not in mcp_client.load_servers():
        app.ui.print(f"[red]No MCP server '{server}' in config.[/red]")
        return
    app.ui.print(f"Connecting to [bold]{server}[/bold]…")
    tools_list = mcp_client.manager.available_tools(server)
    if not tools_list:
        err = mcp_client.manager.error(server)
        app.ui.print(f"[red]Could not list tools for {server}[/red]" + (f": {err}" if err else ""))
        return
    enabled = set(mcp_client.load_enabled().get(server, []))
    app.ui.print(f"[bold]{server}[/bold] — {len(tools_list)} tools:")
    for t in tools_list:
        mark = "[green]x[/green]" if t.name in enabled else " "
        desc = ((t.description or "").strip().splitlines() or [""])[0][:70]
        app.ui.print(f"  [{mark}] {t.name:<30} [dim]{desc}[/dim]")
    app.ui.print(f"  [dim]/mcp enable {server} <tool…|all>[/dim]")


def _mcp_toggle(app, action, server, tool_args):
    if server not in mcp_client.load_servers():
        app.ui.print(f"[red]No MCP server '{server}' in config.[/red]")
        return
    available = {t.name for t in mcp_client.manager.available_tools(server)}
    if not available and action == "enable":
        err = mcp_client.manager.error(server)
        app.ui.print(f"[red]Could not connect to {server}[/red]" + (f": {err}" if err else ""))
        return
    state = mcp_client.load_enabled()
    current = set(state.get(server, []))
    targets = set(available) if (tool_args and tool_args[0].lower() == "all") else set(tool_args)
    if action == "enable":
        unknown = targets - available
        if unknown:
            app.ui.print(f"[red]Unknown tool(s):[/red] {', '.join(sorted(unknown))}. Try /mcp tools {server}.")
            return
        current |= targets
    else:
        current = set() if not tool_args else (current - targets)
    if current:
        state[server] = sorted(current)
    else:
        state.pop(server, None)
    mcp_client.save_enabled(state)
    mcp_client.manager.refresh()
    app.ui.print(f"{server}: [bold]{len(state.get(server, []))}[/bold] tool(s) enabled.")


@command("disconnect")
def _disconnect(rest, app):
    """Remove a saved provider key: /disconnect <provider>."""
    provider = rest.strip().lower()
    if provider not in config.PROVIDER_KEY_ENV:
        app.ui.print(f"[red]Unknown provider '{provider}'.[/red] Known: {', '.join(config.PROVIDER_KEY_ENV)}")
        return
    if config.disconnect(provider):
        app.registry = registry.build_registry()
        app.ui.print(f"Disconnected [bold]{provider}[/bold] and removed its saved key.")
    else:
        app.ui.print(f"{provider} wasn't connected via 2B (a shell env var may still provide it).")


@command("models")
def _models(rest, app):
    """List available models, grouped by provider (scrollable; filter: /models <text>)."""
    reg = registry.usable(app.registry)
    if not reg:
        app.ui.print("[red]No providers configured.[/red] Start Ollama or set a provider API key.")
        return
    needle = rest.strip().lower()

    def emit(out):
        total = 0
        for pname, prov in reg.items():
            try:
                models = prov.list_models()
            except Exception as e:
                out.print(f"  [red]{pname}: {e}[/red]")
                continue
            if needle:
                models = [m for m in models if needle in m.lower()]
            if not models:
                continue
            out.print(f"[bold]{pname}[/bold]")
            for m in models:
                marker = "  (current)" if app.session.default_model in (m, f"{pname}:{m}") else ""
                out.print(f"    {m}{marker}")
            total += len(models)
        if needle and total == 0:
            out.print(f"[dim]No models match '{needle}'.[/dim]")

    # Long lists (OpenRouter/NVIDIA can be hundreds) scroll through the pager.
    with app.ui.pager(styles=True):
        emit(app.ui)


@command("task")
def _task(rest, app):
    """Queue a new task: /task <description>."""
    if not rest.strip():
        app.ui.print("Usage: /task <description>")
        return
    app.enqueue_task(rest.strip())


@command("tasks")
def _tasks(rest, app):
    """List all tasks in this session and their status."""
    if not app.session.tasks:
        app.ui.print("(no tasks yet)")
        return
    for t in app.session.tasks:
        pend, act, done = t.step_counts()
        steps = f"  [{done}✓/{act}▶/{pend}□]" if t.plan_steps else ""
        wait = "  [waiting for confirmation]" if t.pending else ""
        app.ui.print(f"  {t.status_glyph()} [{t.id}] {t.title}  ({t.state.value}){steps}{wait}")


@command("fg")
def _fg(rest, app):
    """Foreground a backgrounded task by id: /fg <id> (needed to approve a task waiting on a write)."""
    if not rest.strip():
        app.ui.print("Usage: /fg <id>  (see /tasks for ids)")
        return
    task = app.session.find(rest.strip())
    if task is None:
        app.ui.print(f"[red]No task with id {rest.strip()}[/red]")
        return
    app.request_foreground(task.id)


@command("steer")
def _steer(rest, app):
    """Redirect the running task mid-turn without stopping it: /steer <text> (typing while it runs does the same)."""
    text = rest.strip()
    if not text:
        app.ui.print("Usage: /steer <text>  — fold a course-correction into the running turn")
        return
    active = app.session.active_task
    if active is None or active.state != TaskState.ACTIVE:
        app.ui.print("Nothing is running to steer — type your message to start a task.")
        return
    active.push_steer(text)
    app.ui.print(f"⤷ steering: {text[:70]}")


@command("tool")
def _tool(rest, app):
    """Invoke a frozen tool directly, bypassing the model: /tool read_file path=a.dart (or {json})."""
    name, parsed = parse_tool_invocation(rest)
    if name is None:
        app.ui.print(parsed)
        return
    run = getattr(app, "run_tool_command", None)
    if run is None:                       # line-mode REPL doesn't offer direct invocation
        app.ui.print("/tool is available in the full-screen TUI.")
        return
    run(name, parsed)


@command("history")
def _history(rest, app):
    """Search the scrollback: /history search <query> — then n / N jump to next / prev match."""
    parts = rest.split(maxsplit=1)
    if not parts or parts[0] != "search" or len(parts) < 2 or not parts[1].strip():
        app.ui.print("Usage: /history search <query>   — then press n / N to jump between matches")
        return
    search = getattr(app, "history_search", None)
    if search is None:
        app.ui.print("/history search is available in the full-screen TUI.")
        return
    search(parts[1].strip())


@command("yes")
def _yes(rest, app):
    """Toggle accept-edits mode (auto-approve writes/edits) for the session."""
    s = app.session
    s.mode = MODE_NORMAL if s.mode == MODE_ACCEPT else MODE_ACCEPT
    app.ui.print(f"Mode: [bold]{MODE_LABELS[s.mode]}[/bold]")


_MODE_ALIASES = {
    "normal": MODE_NORMAL, "confirm": MODE_NORMAL,
    "accept": MODE_ACCEPT, "accept_edits": MODE_ACCEPT, "accept-edits": MODE_ACCEPT,
    "edits": MODE_ACCEPT, "yes": MODE_ACCEPT, "auto": MODE_ACCEPT,
    "plan": MODE_PLAN,
}


@command("mode")
def _mode(rest, app):
    """Set operating mode: /mode [normal|accept|plan] (or shift+tab to cycle)."""
    s = app.session
    name = rest.strip().lower()
    if not name:
        app.ui.print(f"Current mode: [bold]{MODE_LABELS[s.mode]}[/bold].  "
                     "Options: normal (confirm), accept (auto-apply), plan (read-only).")
        return
    target = _MODE_ALIASES.get(name)
    if target is None:
        app.ui.print(f"[red]Unknown mode '{name}'.[/red] Options: normal, accept, plan.")
        return
    s.mode = target
    app.ui.print(f"Mode: [bold]{MODE_LABELS[s.mode]}[/bold]")


@command("sessions")
def _sessions(rest, app):
    """List saved sessions for this project — id, age, model, size (resume from a shell)."""
    from . import persist
    rows = persist.list_sessions(cwd=app.session.cwd)
    if not rows:
        app.ui.print("No saved sessions for this directory yet.")
        return
    app.ui.print("[bold]Saved sessions here[/bold] (newest first):")
    for r in rows:
        age = persist.relative_age(r["updated_at"]) if r.get("updated_at") else ""
        model = (r.get("model") or "").split(":")[-1]     # short model name
        size = f"{r['messages']} msgs" if r.get("messages") else ""
        meta = "  ·  ".join(x for x in (age, model, size) if x)
        app.ui.print(f"  [cyan]{r['id']}[/cyan]  {r['title'] or '(untitled)'}")
        if meta:
            app.ui.print(f"          [dim]{meta}[/dim]")
    app.ui.print("[dim]Resume from a shell: [/dim][cyan]2b --resume <id>[/cyan]"
                 "[dim]  ·  latest here: [/dim][cyan]2b --continue[/cyan]")


def _revert_edit(path, pre, app) -> bool:
    """Restore one recorded edit: rewrite `pre`, or remove a newly-created file
    (pre is None). Prints the outcome; returns True on success."""
    full = tools._safe_path(path)
    if full is None:
        app.ui.print("[red]Cannot undo: empty or invalid path.[/red]")
        return False
    try:
        if pre is None:
            os.remove(full)
            app.ui.print(f"Removed newly-created {path}.")
        else:
            # newline="" restores the snapshot's exact bytes: apply_edit now records
            # `pre` with real line endings, so a CRLF file reverts to CRLF (and text
            # mode wouldn't double a \r\n on Windows).
            with open(full, "w", newline="") as f:
                f.write(pre)
            app.ui.print(f"Reverted {path} to its previous contents.")
        return True
    except OSError as e:
        app.ui.print(f"[red]Undo failed for {path}: {e}[/red]")
        return False


@command("undo")
def _undo(rest, app):
    """Revert recent file edits (multi-level). `/undo` = the last edit; `/undo N` =
    the last N; `/undo <path>` = the most recent edit to that file."""
    task = _target_task(app)
    if task is None or not task.edit_history:
        app.ui.print("Nothing to undo.")
        return
    arg = (rest or "").strip()
    popped = []
    if arg and not arg.isdigit():
        # Undo the most recent edit to a specific file.
        for i in range(len(task.edit_history) - 1, -1, -1):
            if task.edit_history[i][0] == arg:
                popped.append(task.edit_history.pop(i))
                break
        if not popped:
            app.ui.print(f"No recorded edit to {arg}.")
            return
    else:
        n = min(int(arg), len(task.edit_history)) if arg else 1
        popped = [task.edit_history.pop() for _ in range(n)]
    task.last_diff = None
    # Persist the shrunken stack BEFORE touching files: if the process dies mid-undo, the
    # durable log has already dropped these entries, so a resume won't re-revert them
    # (which for a since-recreated file could mean a wrong delete). Worst case is a lost
    # undo, never a wrong action.
    _persist_undo(task, app)
    for path, pre in popped:
        _revert_edit(path, pre, app)


def _persist_undo(task, app) -> None:
    """Keep the durable undo log in sync after a revert, so a resumed session doesn't
    re-offer an edit that's already been undone. Best-effort."""
    try:
        from . import changelog
        changelog.save(task.id, app.session.cwd, task.edit_history)
    except Exception:
        pass


@command("diff")
def _diff(rest, app):
    """Re-show the last proposed/applied diff."""
    task = _target_task(app)
    if task is None or not task.last_diff:
        app.ui.print("No diff available.")
        return
    app.ui.print(task.last_diff)


@command("init")
def _init(rest, app):
    """Scan the project and write 2B.md — a compact map auto-loaded into context on new tasks."""
    root = app.session.cwd
    stack = repomap.detect_stack(root)
    dirs = repomap.top_dirs(root)
    mp = repomap.build_map(root, budget_chars=2800)
    lines = ["# 2B project map", ""]
    if stack:
        lines.append("**Stack:** " + ", ".join(stack))
    if dirs:
        lines.append("**Top-level dirs:** " + ", ".join(dirs))
    lines += ["", "## Symbols (ranked, most central first)", "", mp]
    doc = "\n".join(lines)
    path = os.path.join(root, "2B.md")
    try:
        with open(path, "w") as f:
            f.write(doc)
    except OSError as e:
        app.ui.print(f"[red]Could not write 2B.md: {e}[/red]")
        return
    app.ui.print(f"Wrote [bold]2B.md[/bold] ({len(doc)} chars). It's loaded into context on new tasks, "
                 "so 2B knows the layout instead of hunting for files.")


@command("map")
def _map(rest, app):
    """Show a budget-bounded symbol outline of the project: /map [subdir]."""
    root = app.session.cwd
    arg = rest.strip()
    sub = os.path.join(root, arg)
    if arg and os.path.isdir(sub):
        out = repomap.build_map(sub, budget_chars=4000)
    else:
        out = repomap.build_map(root, budget_chars=4000, focus=arg)
    app.ui.print(out)


@command("add")
def _add(rest, app):
    """Pre-load a file into the current task's context: /add <file>."""
    if not rest.strip():
        app.ui.print("Usage: /add <file>")
        return
    task = _target_task(app)
    if task is None:
        app.ui.print("No task to add context to — start a task first.")
        return
    content = tools.do_read_file(rest.strip())
    if content.startswith("error:"):
        app.ui.print(f"[red]{content}[/red]")
        return
    if task.conversation is None:
        task.conversation = Conversation(system_prompt=orchestrator.SYSTEM_PROMPT)
    task.conversation.append(Message.user(f"[pre-loaded file: {rest.strip()}]\n{content}"))
    app.ui.print(f"Loaded {rest.strip()} into task context ({len(content)} bytes).")


_FETCH_MAX_CHARS = 40_000   # cap page content folded into context (pages can be huge)


@command("fetch")
def _fetch(rest, app):
    """Fetch a web page and pre-load its readable content into the current task's context:
    /fetch <url>. Host-side (like /add) — the model gains no new tool, so it's safe for
    local models too."""
    url = rest.strip()
    if not url:
        app.ui.print("Usage: /fetch <url>")
        return
    task = _target_task(app)
    if task is None:
        app.ui.print("No task to add context to — start a task first.")
        return
    html = web.fetch(url)
    if html is None:
        app.ui.print(f"[red]Could not fetch {url} (offline, blocked, non-HTTPS, or a bad URL).[/red]")
        return
    content = web.extract_readable(html, as_markdown=True, max_chars=_FETCH_MAX_CHARS)
    if not content.strip():
        app.ui.print(f"[red]{url} had no readable text to extract (JS-only page?).[/red]")
        return
    if task.conversation is None:
        task.conversation = Conversation(system_prompt=orchestrator.SYSTEM_PROMPT)
    # Fence the page as untrusted — it's external content that may carry injected
    # instructions; the system prompt tells the model fenced text is data, not commands.
    fenced = untrusted.wrap(content, f"web:{url}")
    task.conversation.append(Message.user(f"[pre-loaded web page: {url}]\n{fenced}"))
    app.ui.print(f"Loaded {url} into task context ({len(content)} chars, {content.count(chr(10)) + 1} lines).")


@command("theme")
def _theme(rest, app):
    """Switch color theme: /theme [system|light|dark] (system = terminal background)."""
    name = rest.strip().lower()
    if not name:
        current = getattr(app, "theme_name", "system")
        app.ui.print(f"Current theme: [bold]{current}[/bold].  Options: system, light, dark.")
        return
    if not hasattr(app, "set_theme"):
        app.ui.print("Themes apply to the full-screen TUI (not [bold]--classic[/bold] mode).")
        return
    app.set_theme(name)


@command("context")
def _context(rest, app):
    """Show estimated context usage for the current task (auto-compacts near the limit)."""
    task = _target_task(app)
    if task is None or task.conversation is None:
        app.ui.print("No conversation yet — start a task first.")
        return
    resolved = registry.resolve(app.registry, task.model_override or app.session.default_model)
    budget = orchestrator.context_budget(resolved[0], resolved[1]) if resolved else 8000
    used = orchestrator.estimate_tokens(task.conversation, getattr(task, "chars_per_token", 4.0))
    pct = int(used / budget * 100) if budget else 0
    at = int(orchestrator.COMPACT_AT * 100)
    app.ui.print(f"Context: ~[bold]{used}[/bold] / {budget} tokens ([bold]{pct}%[/bold]). "
                 f"Auto-compacts at {at}%.")


@command("copy", "cp")
def _copy(rest, app):
    """Copy the last model reply to the clipboard (or hold Option and drag to select)."""
    if hasattr(app, "copy_last"):
        app.copy_last()
    else:
        app.ui.print("In --classic mode, use your terminal's native text selection to copy.")


@command("clear")
def _clear(rest, app):
    """Start fresh — clear the screen, history, and tasks, like a new 2B session."""
    active = app.session.active_task
    if active is not None and active.state == TaskState.ACTIVE:
        app.ui.print("A task is still running — stop it first ([bold]esc[/bold]), then /clear.")
        return
    app.session.tasks.clear()
    app.session.active_task_id = None
    app.session.thread = None       # drop the continuity thread too
    if hasattr(app, "clear_screen"):
        app.clear_screen()          # TUI: wipe the log back to the intro
    else:
        app.ui.print("Cleared.")


@command("new")
def _new(rest, app):
    """Start a new conversation thread, keeping the scrollback on screen."""
    active = app.session.active_task
    if active is not None and active.state == TaskState.ACTIVE:
        app.ui.print("A task is still running — stop it first ([bold]esc[/bold]), then /new.")
        return
    app.session.thread = None       # next message starts a fresh conversation
    app.session.active_task_id = None
    app.ui.print("Started a new thread — the next message won't carry the previous context.")


@command("continuity")
def _continuity(rest, app):
    """Carry conversation context across messages: /continuity on|off (bare toggles)."""
    session = app.session
    resolved = registry.resolve(app.registry, session.default_model)
    is_local = registry.is_local(resolved[0]) if resolved else False
    arg = rest.strip().lower()
    if arg in ("on", "yes", "true"):
        session.continuity_override = True
    elif arg in ("off", "no", "false"):
        session.continuity_override = False
    elif arg == "":                                   # bare: flip the current effective state
        session.continuity_override = not orchestrator._continuity_effective(session, is_local)
    else:
        app.ui.print("Usage: [bold]/continuity on|off[/bold]")
        return
    if orchestrator._continuity_effective(session, is_local):
        note = "  (small local window — leans on compaction; /new to reset)" if is_local else ""
        app.ui.print(f"Continuity [bold]on[/bold] — messages continue the same thread.{note}")
    else:
        session.thread = None                         # detach cleanly so the next message is fresh
        app.ui.print("Continuity [bold]off[/bold] — each message starts a fresh thread.")


_THINK_ALIASES = {"off": "off", "no": "off", "false": "off",
                  "on": "on", "yes": "on", "true": "on",
                  "low": "low", "medium": "medium", "med": "medium", "high": "high"}


@command("think")
def _think(rest, app):
    """Control model reasoning: /think off|on|low|medium|high (bare shows current)."""
    session = app.session
    resolved = registry.resolve(app.registry, session.default_model)
    provider, model = resolved if resolved else (None, "")
    supports = bool(provider and provider.supports_reasoning(model))
    arg = rest.strip().lower()
    if not arg:
        eff = orchestrator._reasoning_effective(session) or "default"
        cap = "supported" if supports else "not supported by the current model"
        app.ui.print(f"Reasoning: [bold]{eff}[/bold] ({cap}).  "
                     "Set with /think off|on|low|medium|high.")
        return
    level = _THINK_ALIASES.get(arg)
    if level is None:
        app.ui.print("[red]Usage:[/red] /think off|on|low|medium|high")
        return
    session.think = level
    note = "" if supports else "  (current model doesn't support reasoning — no effect)"
    app.ui.print(f"Reasoning [bold]{level}[/bold] for this session.{note}")


def _session_conversations(session) -> list[dict]:
    """Every conversation in the session, in task order, deduped by object identity — so a
    shared continuity thread appears once, while detached tasks each contribute their own.
    Each entry is {title, conv, errors}; `errors` collects the failures of every task that
    used that conversation (a stream/tool error lives on the task, not in the messages)."""
    order: list[int] = []
    by_id: dict[int, dict] = {}
    for t in session.tasks:
        conv = t.conversation
        if conv is None:
            continue
        k = id(conv)
        if k not in by_id:
            by_id[k] = {"title": t.title or t.description or "conversation", "conv": conv, "errors": []}
            order.append(k)
        if t.error:
            by_id[k]["errors"].append(t.error)
    return [by_id[k] for k in order]


def _render_message_md(m, lines: list[str]) -> None:
    """Append one message's Markdown to `lines`. Tool calls render as name + a JSON args
    block; results (a user-role turn carrying them) as fenced content, flagged on error."""
    role = m.role.value
    if role == "system":
        return                                        # boilerplate, not part of the conversation
    if m.tool_results:
        for r in m.tool_results:
            lines += ["", "⚠ error:" if r.is_error else "→ result:", "````", r.content or "", "````"]
        return
    if role == "user":
        lines += ["", "## You", "", m.text or ""]
        return
    lines += ["", "## Assistant"]                     # assistant turn
    if m.thinking and m.thinking.strip():
        lines += ["", "> _thinking:_"]
        lines += [f"> {tl}" for tl in m.thinking.strip().splitlines()]
    if m.text and m.text.strip():
        lines += ["", m.text.strip()]
    for c in m.tool_calls:
        lines += ["", f"**⚙ {c.name}**", "```json", json.dumps(c.arguments, ensure_ascii=False), "```"]


def _render_session_md(session) -> tuple[str, int, int]:
    """Render the whole session to Markdown; returns (text, message_count, error_count).
    Task errors (a failed turn that produced no assistant message — a provider 4xx/5xx, a
    tool crash) are rendered after their conversation so the export shows why it ended."""
    convs = _session_conversations(session)
    orphan = [t.error for t in session.tasks if t.error and t.conversation is None]  # failed pre-conversation
    total = sum(len(e["conv"].messages) for e in convs)
    errs = sum(len(e["errors"]) for e in convs) + len(orphan)
    head = f"- {len(convs)} conversation(s) · {total} messages" + (f" · {errs} error(s)" if errs else "")
    lines = ["# 2B session export", "",
             f"- Model: {session.default_model or '(unset)'}",
             f"- Exported: {time.strftime('%Y-%m-%d %H:%M:%S')}", head]
    for idx, e in enumerate(convs):
        lines += ["", "---"]
        if len(convs) > 1:
            lines += ["", f"## — conversation {idx + 1}: {e['title']} —"]
        for m in e["conv"].messages:
            _render_message_md(m, lines)
        for err in e["errors"]:
            lines += ["", f"**⚠ task error:** {err}"]
    if orphan:
        lines += ["", "---", "", "## Errors"]
        for err in orphan:
            lines += ["", f"**⚠ task error:** {err}"]
    return "\n".join(lines) + "\n", total, errs


@command("export")
def _export(rest, app):
    """Export the whole session, tool calls included, to a Markdown file: /export [path]."""
    md, total, errs = _render_session_md(app.session)
    if total == 0 and errs == 0:
        app.ui.print("Nothing to export yet — the session is empty.")
        return
    arg = rest.strip()
    if arg:
        path = os.path.expanduser(arg)
        if not os.path.isabs(path):
            path = os.path.join(app.session.cwd, path)
    else:
        path = os.path.join(app.session.cwd, f"2b-session-{time.strftime('%Y%m%d-%H%M%S')}.md")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
    except OSError as e:
        app.ui.print(f"[red]Export failed:[/red] {e}")
        return
    suffix = f" ({errs} error(s))" if errs else ""
    app.ui.print(f"Exported {total} messages{suffix} to [bold]{path}[/bold]")


@command("quit", "q", "exit")
def _quit(rest, app):
    """Exit 2B."""
    app.request_quit()
