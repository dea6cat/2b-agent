"""Slash-command dispatch. Any input starting with '/' is handled here on the
UI thread and never reaches the model. Handlers operate on the App controller
(duck-typed: .session, .console, and task-control methods) to avoid a circular
import with cli.py.
"""
import os

from . import config, mcp_client, orchestrator, registry, repomap, tools
from .conversation import Conversation, Message
from .session import MODE_ACCEPT, MODE_NORMAL, MODE_PLAN, MODE_LABELS, TaskState

COMMANDS = {}


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
    used = orchestrator.estimate_tokens(task.conversation)
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
    if hasattr(app, "clear_screen"):
        app.clear_screen()          # TUI: wipe the log back to the intro
    else:
        app.ui.print("Cleared.")


@command("quit", "q", "exit")
def _quit(rest, app):
    """Exit 2B."""
    app.request_quit()
