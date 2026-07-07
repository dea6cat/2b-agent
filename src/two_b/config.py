"""Provider API keys: the map of provider -> env var, plus optional on-disk
persistence so keys entered with /connect survive across sessions.

Providers read their keys lazily from os.environ (see providers/*.py), so
"connecting" a provider is just setting the right env var — and, if the user
wants it to stick, writing it to ~/.config/2b/keys.json (chmod 600). A key
exported in the real shell environment always wins over a saved one.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Provider name -> the environment variable its adapter reads. (Ollama's key is
# only for Ollama Cloud; local Ollama needs none.)
PROVIDER_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "ollama": "OLLAMA_API_KEY",
}

CONFIG_DIR = Path(os.path.expanduser("~/.config/2b"))
KEYS_FILE = CONFIG_DIR / "keys.json"
PREFS_FILE = CONFIG_DIR / "prefs.json"     # non-secret settings, e.g. the persisted default model


def _load() -> dict:
    try:
        data = json.loads(KEYS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    KEYS_FILE.write_text(json.dumps(data, indent=2))
    try:
        KEYS_FILE.chmod(0o600)
    except OSError:
        pass


def load_into_env() -> None:
    """Populate os.environ from saved keys at startup, without clobbering a key
    the user has already exported in their shell (that one is authoritative)."""
    for provider, key in _load().items():
        env = PROVIDER_KEY_ENV.get(provider)
        if env and key:
            os.environ.setdefault(env, key)


def connect(provider: str, key: str) -> None:
    """Set the provider's key for this session and persist it for future ones."""
    env = PROVIDER_KEY_ENV[provider]
    os.environ[env] = key
    data = _load()
    data[provider] = key
    _write(data)


def disconnect(provider: str) -> bool:
    """Remove a saved key and unset it for this session. True if one was saved."""
    data = _load()
    existed = provider in data
    if existed:
        del data[provider]
        _write(data)
    env = PROVIDER_KEY_ENV.get(provider)
    if env:
        os.environ.pop(env, None)
    return existed


def is_connected(provider: str) -> bool:
    env = PROVIDER_KEY_ENV.get(provider)
    if not env:
        return False
    if os.environ.get(env):
        return True
    return provider == "google" and bool(os.environ.get("GOOGLE_API_KEY"))


def saved_providers() -> set:
    return set(_load().keys())


def get_prefs() -> dict:
    """Non-secret persisted preferences (currently just the default model). {} if none."""
    try:
        data = json.loads(PREFS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def set_pref(key: str, value) -> None:
    """Persist one preference key (value=None removes it), leaving the rest intact."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = get_prefs()
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    PREFS_FILE.write_text(json.dumps(data, indent=2))


def mask(key: str) -> str:
    k = (key or "").strip()
    if len(k) <= 8:
        return "•" * len(k)
    return f"{k[:4]}…{k[-4:]}"
