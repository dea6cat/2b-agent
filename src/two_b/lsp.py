"""Minimal LSP client — stdlib JSON-RPC over stdio, no SDK (matches the urllib ethos).

A host-side *semantic* backend for symbols.py: when a language server for a project's
language is installed, `definitions()` resolves a symbol with real, scope/import-aware
results; otherwise it returns None and the regex floor takes over. Never a model-facing
tool — the five-tool schema is unchanged.

Robustness is the contract. Every path that can't produce an answer returns None so the
resolver falls through to regex, and nothing here can stall a tool for long (per-request
deadline) or crash it (a background reader thread + best-effort teardown). Opt out with
TWOB_NO_LSP.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

REQUEST_TIMEOUT = 6        # per-request cap; a cold/slow server just falls back to regex
INIT_TIMEOUT = 8           # initialize handshake budget
_MAX_PROJECT_SCAN = 4000   # files sniffed when detecting a project's languages

# ext -> ordered (which-binary, argv, languageId) candidates; first installed wins.
_SERVER_CANDIDATES: dict[str, list[tuple[str, list[str], str]]] = {
    ".dart": [("dart", ["dart", "language-server"], "dart")],
    ".py": [("pyright-langserver", ["pyright-langserver", "--stdio"], "python"),
            ("pylsp", ["pylsp"], "python")],
    ".ts": [("typescript-language-server", ["typescript-language-server", "--stdio"], "typescript")],
    ".go": [("gopls", ["gopls"], "go")],
    ".rs": [("rust-analyzer", ["rust-analyzer"], "rust")],
    ".c": [("clangd", ["clangd"], "c")],
    ".cpp": [("clangd", ["clangd"], "cpp")],
}
_SERVER_CANDIDATES[".tsx"] = _SERVER_CANDIDATES[".ts"]
_SERVER_CANDIDATES[".js"] = _SERVER_CANDIDATES[".jsx"] = [
    ("typescript-language-server", ["typescript-language-server", "--stdio"], "javascript")]
_SERVER_CANDIDATES[".cc"] = _SERVER_CANDIDATES[".h"] = _SERVER_CANDIDATES[".hpp"] = _SERVER_CANDIDATES[".cpp"]

_PROJECT_MARKERS = ("pubspec.yaml", "pyproject.toml", "setup.py", "package.json",
                    "go.mod", "Cargo.toml", ".git")


# --- pure wire framing (unit-testable without a real server) ----------------

def encode(msg: dict) -> bytes:
    body = json.dumps(msg).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def read_frame(stream) -> dict | None:
    """Read one Content-Length-framed JSON-RPC message from a binary stream. Returns
    None on EOF or a malformed frame."""
    length = None
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:                       # blank line ends the header block
            break
        if line.lower().startswith(b"content-length:"):
            try:
                length = int(line.split(b":", 1)[1])
            except ValueError:
                return None
    if length is None:
        return None
    body = stream.read(length)
    if not body or len(body) < length:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _server_spec(ext: str):
    for name, argv, lang in _SERVER_CANDIDATES.get(ext, []):
        if shutil.which(name):
            return tuple(argv), lang
    return None


def _to_uri(path: str) -> str:
    return Path(os.path.abspath(path)).as_uri()


def _from_uri(uri: str) -> str | None:
    if not uri or not uri.startswith("file:"):
        return None
    return url2pathname(urlparse(uri).path)


def _project_root(path: str) -> str:
    d = os.path.dirname(os.path.abspath(path))
    cur = d
    while True:
        if any(os.path.exists(os.path.join(cur, m)) for m in _PROJECT_MARKERS):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return d
        cur = parent


# --- server process + JSON-RPC transport ------------------------------------

class _Server:
    def __init__(self, argv: tuple[str, ...], root: str, lang: str):
        self.lang = lang
        self._proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, cwd=root, bufsize=0)
        self._wlock = threading.Lock()
        self._id = 0
        self._pending: dict[int, threading.Event] = {}
        self._responses: dict[int, dict] = {}
        self._closed = False
        self._opened: set[str] = set()
        threading.Thread(target=self._reader, daemon=True).start()
        self.ok = self._initialize(root)

    def _write(self, msg: dict):
        with self._wlock:
            try:
                self._proc.stdin.write(encode(msg))
                self._proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                self._closed = True

    def _reader(self):
        out = self._proc.stdout
        while not self._closed:
            msg = read_frame(out)
            if msg is None:
                break
            mid = msg.get("id")
            if mid is not None and ("result" in msg or "error" in msg):      # our response
                self._responses[mid] = msg
                ev = self._pending.get(mid)
                if ev:
                    ev.set()
            elif mid is not None and "method" in msg:                        # server->client request
                self._write({"jsonrpc": "2.0", "id": mid, "result": None})   # reply so it doesn't block
            # notifications (no id) are ignored

    def request(self, method: str, params: dict, timeout: float):
        if self._closed:
            return None
        self._id += 1
        rid = self._id
        ev = threading.Event()
        self._pending[rid] = ev
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        got = ev.wait(timeout)
        self._pending.pop(rid, None)
        resp = self._responses.pop(rid, None)
        if not got or resp is None or "error" in resp:
            return None
        return resp.get("result")

    def notify(self, method: str, params: dict):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _initialize(self, root: str) -> bool:
        res = self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": _to_uri(root),
            "capabilities": {},
            "workspaceFolders": [{"uri": _to_uri(root), "name": os.path.basename(root) or "root"}],
        }, INIT_TIMEOUT)
        if res is None:
            return False
        self.notify("initialized", {})
        return True

    def open(self, path: str) -> str | None:
        uri = _to_uri(path)
        if uri in self._opened:
            return uri
        try:
            with open(path, "r", errors="ignore") as f:
                text = f.read()
        except OSError:
            return None
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": self.lang, "version": 1, "text": text}})
        self._opened.add(uri)
        return uri

    def shutdown(self):
        self._closed = True
        try:
            self._proc.terminate()
        except (ProcessLookupError, OSError):
            pass


# --- server cache (one per argv+root), torn down at exit --------------------

_ACTIVE: dict[tuple, _Server] = {}
_ACTIVE_LOCK = threading.Lock()


def _get_server(argv: tuple, root: str, lang: str) -> _Server | None:
    key = (argv, os.path.abspath(root))
    with _ACTIVE_LOCK:
        srv = _ACTIVE.get(key)
        if srv is not None and not srv._closed:
            return srv if srv.ok else None
        try:
            srv = _Server(argv, root, lang)
        except (FileNotFoundError, OSError):
            return None
        _ACTIVE[key] = srv
        return srv if srv.ok else None


def shutdown_all():
    with _ACTIVE_LOCK:
        for srv in _ACTIVE.values():
            srv.shutdown()
        _ACTIVE.clear()


atexit.register(shutdown_all)


def _project_langs(cwd: str):
    """Installed server specs for the languages that actually appear under cwd."""
    from .tools import _should_skip_dir, _should_skip_file
    seen, specs, scanned = set(), [], 0
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        for fn in filenames:
            scanned += 1
            if scanned > _MAX_PROJECT_SCAN:
                return specs
            ext = os.path.splitext(fn)[1].lower()
            if ext in seen or _should_skip_file(fn) or ext not in _SERVER_CANDIDATES:
                continue
            seen.add(ext)
            spec = _server_spec(ext)
            if spec and spec not in specs:
                specs.append(spec)
    return specs


# --- the symbols.py-facing backend ------------------------------------------

def definitions(identifier: str, cwd: str):
    """Semantic definition sites for `identifier` via workspace/symbol on whatever
    language servers the project uses. Returns a non-empty list of (relpath, line,
    name) tuples, or None to defer to the regex floor (no server, or nothing found)."""
    if os.environ.get("TWOB_NO_LSP"):
        return None
    specs = _project_langs(cwd)
    if not specs:
        return None
    from .symbols import Loc
    locs = []
    for argv, lang in specs:
        srv = _get_server(argv, cwd, lang)
        if srv is None:
            continue
        for si in srv.request("workspace/symbol", {"query": identifier}, REQUEST_TIMEOUT) or []:
            if si.get("name") != identifier:
                continue
            loc = si.get("location") or {}
            path = _from_uri(loc.get("uri"))
            if not path:
                continue
            line = (loc.get("range", {}).get("start", {}).get("line", 0)) + 1
            locs.append(Loc(os.path.relpath(path, cwd), line, identifier))
    return locs or None       # empty => let regex try (server may just be cold)
