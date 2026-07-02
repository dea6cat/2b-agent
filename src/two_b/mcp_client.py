"""MCP (Model Context Protocol) integration.

2B's whole premise is that small local models break when flooded with tools, so
MCP tools are **opt-in and curated per tool**, never dumped wholesale: you enable
a server and pick exactly which of its tools reach the model. Enabled tools are
merged into the tool schema as namespaced `server__tool` entries and routed back
to the right server when the model calls them.

Transport is stdio only (local subprocess servers like dart / mempalace), via the
official `mcp` SDK. The SDK is asyncio; 2B's turn loop is synchronous on worker
threads, so a single background event loop runs here and calls are marshalled onto
it with run_coroutine_threadsafe.

Config (reused, Claude-Code-style `mcpServers`) is read from, in order of
precedence: ./.mcp.json (project), then ~/.config/2b/mcp.json. Per-tool curation
lives in ~/.config/2b/mcp_enabled.json ({server: [tool, ...]}).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from pathlib import Path

from .toolspec import ToolSpec

CONFIG_DIR = Path(os.path.expanduser("~/.config/2b"))
ENABLED_FILE = CONFIG_DIR / "mcp_enabled.json"
_CONFIG_PATHS = [Path.cwd() / ".mcp.json", CONFIG_DIR / "mcp.json"]
_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")

CONNECT_TIMEOUT = 25
CALL_TIMEOUT = 120


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_servers() -> dict:
    """Merged {name: {command, args, env}} from all config locations (project wins)."""
    merged: dict = {}
    for path in reversed(_CONFIG_PATHS):        # lowest precedence first
        servers = _read_json(path).get("mcpServers", {})
        if isinstance(servers, dict):
            merged.update(servers)
    return merged


def load_enabled() -> dict:
    data = _read_json(ENABLED_FILE)
    return {k: list(v) for k, v in data.items() if isinstance(v, list)}


def save_enabled(state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ENABLED_FILE.write_text(json.dumps(state, indent=2))


class McpManager:
    """Owns the background event loop and one ClientSession per connected server."""

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._sessions: dict = {}       # server -> ClientSession
        self._stacks: dict = {}         # server -> AsyncExitStack
        self._tools: dict = {}          # server -> {tool_name: Tool}
        self._errors: dict = {}         # server -> error string

    # --- event loop plumbing ------------------------------------------------
    def _ensure_loop(self) -> None:
        with self._lock:
            if self._loop is not None:
                return
            loop = asyncio.new_event_loop()
            t = threading.Thread(target=loop.run_forever, daemon=True, name="mcp-loop")
            t.start()
            self._loop, self._thread = loop, t

    def _run(self, coro, timeout):
        self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    # --- connection ---------------------------------------------------------
    async def _connect(self, name: str, cfg: dict) -> None:
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        stack = AsyncExitStack()
        params = StdioServerParameters(
            command=cfg["command"],
            args=list(cfg.get("args", [])),
            env={**os.environ, **cfg.get("env", {})},
        )
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        listed = await session.list_tools()
        self._sessions[name] = session
        self._stacks[name] = stack
        self._tools[name] = {t.name: t for t in listed.tools}
        self._errors.pop(name, None)

    def connect(self, name: str) -> bool:
        """Connect one server (idempotent). Returns True on success."""
        if name in self._sessions:
            return True
        cfg = load_servers().get(name)
        if not cfg or not cfg.get("command"):
            self._errors[name] = "no such server in mcp.json"
            return False
        try:
            self._run(self._connect(name, cfg), CONNECT_TIMEOUT)
            return True
        except Exception as e:
            self._errors[name] = str(e)[:200]
            return False

    def start(self) -> None:
        """Connect every server that has at least one enabled tool (called once at
        startup). No-op — and zero cost — when nothing is enabled."""
        for name, tools in load_enabled().items():
            if tools and name in load_servers():
                self.connect(name)

    def refresh(self) -> None:
        """Re-sync connections with the current curation state (after /mcp changes)."""
        self.start()

    # --- introspection ------------------------------------------------------
    def available_tools(self, server: str) -> list:
        """Tool objects for a server, connecting on demand. [] if unavailable."""
        if server not in self._tools and not self.connect(server):
            return []
        return list(self._tools.get(server, {}).values())

    def error(self, server: str) -> str:
        return self._errors.get(server, "")

    # --- what the model sees ------------------------------------------------
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        """Curated, namespaced ToolSpecs for every enabled+connected tool."""
        specs: list[ToolSpec] = []
        enabled = load_enabled()
        for server, names in enabled.items():
            if not names or server not in self._sessions:
                continue
            catalog = self._tools.get(server, {})
            for tname in names:
                tool = catalog.get(tname)
                if tool is None:
                    continue
                schema = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}
                specs.append(ToolSpec(
                    name=f"{_NAME_RE.sub('_', server)}__{tname}",
                    description=(tool.description or "")[:1024],
                    raw_schema=schema,
                ))
        return tuple(specs)

    def is_mcp_tool(self, qualified: str) -> bool:
        server = qualified.split("__", 1)[0]
        return "__" in qualified and server in self._sessions

    def call_tool(self, qualified: str, args: dict, timeout: float = CALL_TIMEOUT) -> str:
        server, _, tool = qualified.partition("__")
        session = self._sessions.get(server)
        if session is None:
            return f"error: MCP server '{server}' is not connected"
        try:
            res = self._run(session.call_tool(tool, args or {}), timeout)
        except Exception as e:
            return f"error: MCP call {qualified} failed: {str(e)[:200]}"
        return self._flatten(res)

    @staticmethod
    def _flatten(res) -> str:
        parts = []
        for c in (getattr(res, "content", None) or []):
            text = getattr(c, "text", None)
            parts.append(text if text is not None else str(getattr(c, "data", c)))
        out = "\n".join(p for p in parts if p) or "(no output)"
        return f"error: {out}" if getattr(res, "isError", False) else out

    # --- host-consumed symbol resolution (a symbols.py backend) -------------
    def _find_resolver(self):
        """An enabled+connected tool that resolves workspace symbols, if any:
        (server, tool_name, tool_obj). Matches names like `resolve_workspace_symbol`."""
        for server, names in load_enabled().items():
            if server not in self._sessions:
                continue
            catalog = self._tools.get(server, {})
            for tname in names:
                low = tname.lower().replace("-", "").replace("_", "")
                if "symbol" in low and ("resolve" in low or "workspace" in low):
                    return server, tname, catalog.get(tname)
        return None

    def resolve_symbol(self, identifier: str, timeout: float = 8):
        """Host-side (never model-facing) symbol resolution via an enabled MCP resolver
        tool. Returns [(path, line), …] parsed from its output, or None when there's no
        resolver, the call fails, or nothing parses — so symbols.py falls to regex.
        Best-effort: the result text format is server-specific, so parsing is lenient."""
        found = self._find_resolver()
        if not found:
            return None
        server, tname, tool = found
        text = self.call_tool(f"{server}__{tname}", {_resolver_arg(tool): identifier}, timeout)
        if text.startswith("error:"):
            return None
        return _parse_locations(text) or None

    def shutdown(self) -> None:
        for name in list(self._stacks):
            try:
                self._run(self._stacks[name].aclose(), 10)
            except Exception:
                pass
        self._stacks.clear(); self._sessions.clear(); self._tools.clear()


_LOC_RE = re.compile(r"([\w./\\-]+\.\w+):(\d+)")   # file.ext:line, tolerating an optional :col after


def _parse_locations(text: str, cap: int = 10) -> list[tuple[str, int]]:
    """Pull (path, line) pairs out of a resolver tool's flattened text output."""
    hits, seen = [], set()
    for m in _LOC_RE.finditer(text):
        key = (m.group(1), int(m.group(2)))
        if key in seen:
            continue
        seen.add(key)
        hits.append(key)
        if len(hits) >= cap:
            break
    return hits


def _resolver_arg(tool) -> str:
    """The query parameter name a resolver tool expects, read from its inputSchema."""
    schema = getattr(tool, "inputSchema", None) or {}
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    for cand in ("query", "name", "symbol", "pattern", "text"):
        if cand in props:
            return cand
    req = schema.get("required") if isinstance(schema, dict) else None
    return req[0] if req else "query"


manager = McpManager()
