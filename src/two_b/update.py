"""Best-effort update check for 2b-agent — background, cached, offline-safe.

The design keeps startup instant and offline-friendly: on launch, `notice()` prints any
update found by a *previous* background check (read from a cache file — no network on the
hot path), then, at most once a day, spawns a daemon thread to refresh the check for next
time. So notices appear one launch late, startup never blocks, and being offline just means
the refresh silently fails. Never raises.

Source of truth is the repo's git tags (the project ships tagged releases). Opt out with
TWOB_NO_UPDATE_CHECK; apply an update with `2b --update` (see run_upgrade).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

from . import __version__, config

CACHE = config.CONFIG_DIR / "update_check.json"
CHECK_INTERVAL = 86400          # refresh the check at most once a day
FETCH_TIMEOUT = 3               # short — this runs off-thread, but never hang forever
TAGS_URL = "https://api.github.com/repos/dea6cat/2b-agent/tags"
PKG = "2b-agent"


def _parse_ver(s: str) -> tuple:
    """Lenient 'v0.2.0' -> (0, 2, 0). Non-numeric junk in a segment stops that segment."""
    out = []
    for part in str(s).lstrip("vV").split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        out.append(int(num) if num else 0)
    return tuple(out) or (0,)


def _read_cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return {}


def _write_cache(data: dict) -> None:
    try:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data))
    except OSError:
        pass


def _fetch_latest() -> str | None:
    req = urllib.request.Request(
        TAGS_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": PKG})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        tags = json.loads(r.read())
    names = [t.get("name", "") for t in tags if isinstance(t, dict)]
    versioned = [n for n in names if _parse_ver(n) != (0,)]
    return max(versioned, key=_parse_ver) if versioned else None


def _refresh(now: float) -> None:
    try:
        latest = _fetch_latest()
    except Exception:
        latest = None
    data = _read_cache()
    data["last_check"] = now
    if latest:
        data["latest"] = latest
    _write_cache(data)


def notice(now: float | None = None) -> str | None:
    """Return a one-line 'update available' string if a newer tag is already known (from a
    prior check), else None — and kick off a throttled background refresh for next time.
    Never blocks, never raises."""
    if os.environ.get("TWOB_NO_UPDATE_CHECK"):
        return None
    now = time.time() if now is None else now
    data = _read_cache()
    latest = data.get("latest")
    msg = None
    if latest and _parse_ver(latest) > _parse_ver(__version__):
        msg = f"update available: {latest} (you have {__version__}) — run: 2b --update"
    if now - float(data.get("last_check", 0)) > CHECK_INTERVAL:
        try:
            threading.Thread(target=_refresh, args=(now,), daemon=True).start()
        except Exception:
            pass
    return msg


def _kind_from(paths: str) -> str:
    """Classify an install from where its files live: uv tool, pipx, Homebrew, or plain pip."""
    p = paths.replace(os.sep, "/").lower()
    if "/uv/tools/" in p:
        return "uv"
    if "/pipx/" in p:
        return "pipx"
    if "/cellar/" in p:        # Homebrew formula: <prefix>/Cellar/<formula>/<version>/…
        return "brew"
    return "pip"


def _install_kind() -> str:
    """How this 2b-agent was installed, inferred from its run location."""
    return _kind_from(sys.prefix + "|" + os.path.abspath(__file__))


def run_upgrade(emit) -> int:
    """`2b --update`: upgrade using whatever installed it — `uv tool upgrade` (installer/
    uv), `pipx upgrade` (pipx), `brew upgrade` (Homebrew), or `pip install -U` (pip). Returns
    the tool's exit code (1 if the needed tool isn't found). Lets its progress print live."""
    kind = _install_kind()
    if kind == "uv":
        if not shutil.which("uv"):
            emit(f"uv not found — run 'uv tool upgrade {PKG}' once it's on PATH.")
            return 1
        emit(f"Updating {PKG} via uv tool…")
        cmd = ["uv", "tool", "upgrade", PKG]
    elif kind == "pipx":
        if not shutil.which("pipx"):
            emit(f"pipx not found — run 'pipx upgrade {PKG}' once it's on PATH.")
            return 1
        emit(f"Updating {PKG} via pipx…")
        cmd = ["pipx", "upgrade", PKG]
    elif kind == "brew":
        if not shutil.which("brew"):
            emit(f"brew not found — run 'brew upgrade {PKG}' once it's on PATH.")
            return 1
        emit(f"Updating {PKG} via Homebrew…")
        cmd = ["brew", "upgrade", PKG]
    else:
        emit(f"Updating {PKG} via pip…")
        cmd = [sys.executable, "-m", "pip", "install", "-U", PKG]
    try:
        return subprocess.run(cmd, timeout=600).returncode
    except Exception as e:
        emit(f"update failed: {e}")
        return 1
