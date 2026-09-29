"""Per-model capability catalog for cloud models.

A small bundled JSON (`catalog.json`) maps well-known cloud model ids to their
real context window, output-token cap, and image support, so 2B budgets and
auto-compacts against each model's actual window instead of a coarse
per-provider constant. Keys are matched by longest prefix, so dated/suffixed
variants (e.g. `gpt-4o-2024-08-06`) resolve to their base entry. Local Ollama
models are absent by design — their window is sized dynamically from `num_ctx`.

A missing or corrupt catalog degrades to "unknown" (callers fall back to their
own defaults); it must never crash startup.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_CATALOG_PATH = Path(__file__).with_name("catalog.json")


@dataclass(frozen=True)
class ModelInfo:
    context_window: int          # input-token window 2B budgets/compacts against
    default_max_tokens: int      # output-token cap
    supports_images: bool        # informational; 2B has no image path yet


_cache: dict[str, ModelInfo] | None = None


def _load() -> dict[str, ModelInfo]:
    """Parse and cache the catalog, keyed by lowercased model prefix. Any error
    (missing file, bad JSON, malformed entry) yields an empty table."""
    global _cache
    if _cache is not None:
        return _cache
    table: dict[str, ModelInfo] = {}
    try:
        data = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
        for key, v in (data.get("models") or {}).items():
            table[key.lower()] = ModelInfo(
                context_window=int(v["context_window"]),
                default_max_tokens=int(v["default_max_tokens"]),
                supports_images=bool(v.get("supports_images", False)),
            )
    except Exception:
        table = {}
    _cache = table
    return table


def lookup(model: str) -> ModelInfo | None:
    """The catalog entry for a model id, matched by longest key prefix, or None.
    e.g. 'gpt-4o-mini' -> the 'gpt-4o-mini' entry; 'gpt-4o-2024-08-06' -> 'gpt-4o'."""
    if not model:
        return None
    m = model.lower()
    table = _load()
    exact = table.get(m)
    if exact is not None:
        return exact
    best_key = ""
    for key in table:
        if m.startswith(key) and len(key) > len(best_key):
            best_key = key
    return table.get(best_key) if best_key else None


def context_window(model: str) -> int | None:
    """The model's context window, or None when it isn't catalogued."""
    info = lookup(model)
    return info.context_window if info else None


def max_tokens(model: str, default: int) -> int:
    """The model's output-token cap, or `default` when it isn't catalogued."""
    info = lookup(model)
    return info.default_max_tokens if info else default


def supports_images(model: str) -> bool:
    """Whether the model accepts image input (False when not catalogued)."""
    info = lookup(model)
    return bool(info and info.supports_images)
