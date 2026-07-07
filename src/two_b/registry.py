"""Provider registry + model resolution.

Builds every adapter 2B knows about; `usable()` filters to those actually
configured (Ollama reachable / API key present). Resolution maps a model string
to (provider, model): explicit `provider:model`, or a bare name matched by
membership across usable providers (ambiguity requires the explicit form).

Adding an OpenAI-compatible service is data, not code — append to _OPENAI_COMPAT.
"""
from __future__ import annotations

from .providers import ollama
from .providers.base import Provider
from .providers.openai_compat import OpenAICompatProvider

# name, base_url, key_env, dynamic_models, static models (only for dynamic_models=False —
# a dynamic provider lists live via /models and never falls back to a hardcoded guess), extra headers
_OPENAI_COMPAT = [
    ("openai", "https://api.openai.com/v1", "OPENAI_API_KEY", True, [], {}),
    ("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", True, [],
     {"HTTP-Referer": "https://github.com/dea6cat/2b-agent", "X-Title": "2B Agent"}),
    ("mistral", "https://api.mistral.ai/v1", "MISTRAL_API_KEY", True, [], {}),
    ("nvidia", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY", True, [], {}),
    ("deepseek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", True, [], {}),
    ("cerebras", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", True, [], {}),
]


def build_registry() -> dict[str, Provider]:
    reg: dict[str, Provider] = {}
    reg["ollama"] = ollama.local()
    cloud = ollama.cloud()
    if cloud is not None:
        reg[cloud.name] = cloud
    for name, base, key_env, dyn, models, hdrs in _OPENAI_COMPAT:
        reg[name] = OpenAICompatProvider(name, base, key_env, models=models,
                                         dynamic_models=dyn, extra_headers=hdrs)
    # Anthropic and Google adapters register here as they land (M3.5 / M3.6).
    try:
        from .providers.anthropic import AnthropicProvider
        reg["anthropic"] = AnthropicProvider()
    except Exception:
        pass
    try:
        from .providers.google import GoogleProvider
        reg["google"] = GoogleProvider()
    except Exception:
        pass
    return reg


def usable(reg: dict[str, Provider]) -> dict[str, Provider]:
    return {name: p for name, p in reg.items() if p.is_available()}


def is_local(provider: Provider) -> bool:
    """True for the local Ollama provider (no API key). Everything else — Ollama
    Cloud, OpenAI, Anthropic, … — is a cloud provider."""
    return getattr(provider, "name", "") == "ollama" and getattr(provider, "api_key", None) is None


def resolve(reg: dict[str, Provider], model: str) -> tuple[Provider, str] | None:
    """Return (provider, model) or None. Explicit 'provider:model' wins; else a
    bare name is matched across *usable* providers (ambiguity -> None)."""
    if ":" in model:
        prefix, rest = model.split(":", 1)
        if prefix in reg:
            p = reg[prefix]
            return (p, rest) if p.is_available() else None
    live = usable(reg)
    matches = []
    for p in live.values():
        try:
            if model in p.list_models():
                matches.append(p)
        except Exception:
            continue
    if len(matches) == 1:
        return matches[0], model
    return None  # unresolved or ambiguous
