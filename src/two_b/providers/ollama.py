"""Ollama adapter — native /api/chat, NEVER the /v1 OpenAI-compat shim.

Handles both local Ollama (default, no key) and Ollama Cloud (ollama.com with an
API key). This is the load-bearing rule of the whole project: local models stay
on Ollama's own protocol, because the /v1 translation measurably degrades their
tool selection. The serialized payload here is byte-equivalent to the validated
prototype's.
"""
import os

import json as _json
from typing import Callable

from ..conversation import Conversation, Message, Role, ToolCall
from ..tools import recover_toolcalls
from ..toolspec import ToolSpec, to_openai
from .base import Provider, ProviderResponse, get_json, post_json, post_stream

LOCAL_DEFAULT = "http://localhost:11434"
CLOUD_HOST = "https://ollama.com"

# Keep the model — and, crucially, its prompt-prefix KV cache — resident between turns.
# Ollama's default is 5m; a coding turn's thinking can exceed that, and an unload drops
# the cached stable prefix (system prompt + tool schema), forcing a full re-encode next
# turn. 30m spans a normal session so back-to-back turns reuse the warm prefix.
KEEP_ALIVE = "30m"

CTX_FALLBACK = 16384     # used when RAM/arch can't be read to size the window
CLOUD_CTX = 120_000      # cloud runs large windows; don't pin num_ctx there
CTX_FLOOR = 2048         # never pin below this
CTX_ROUND = 1024         # round the computed window down to a clean multiple
KV_RESERVE_BYTES = 3 * 1024 ** 3   # RAM kept free for OS + app + compute buffers
KV_USE_FRACTION = 0.75             # of the RAM left after weights+reserve, give this to KV


def _parse_args(args):
    """Tool-call arguments as a dict. A small local model sometimes emits the
    arguments as a (occasionally malformed) JSON string; a broken string must not
    raise here and abort the task — it becomes {} so the host-side coercion +
    required-arg check can hand the model a recoverable error instead. Mirrors the
    guard the OpenAI-compatible adapter already applies."""
    if isinstance(args, str):
        try:
            args = _json.loads(args)
        except (ValueError, TypeError):
            return {}
    return args if isinstance(args, dict) else {}


def _total_ram_bytes() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")   # macOS + Linux
    except (ValueError, OSError, AttributeError):
        return 0


def _mi_int(model_info: dict, suffix: str):
    """First int field whose key ends with suffix, ignoring vision-tower keys."""
    for k, v in model_info.items():
        if ".vision." not in k and k.endswith(suffix) and isinstance(v, int):
            return v
    return None


def _kv_bytes_per_token(mi: dict) -> int:
    """f16 KV-cache bytes per token = layers × kv_heads × (k_len + v_len) × 2.
    Works across model families (llama/qwen/gemma/…); 0 if the arch is unreadable."""
    layers = _mi_int(mi, ".block_count")
    heads = _mi_int(mi, ".attention.head_count")
    kv_heads = _mi_int(mi, ".attention.head_count_kv") or heads    # GQA if present, else full
    klen = _mi_int(mi, ".attention.key_length")
    vlen = _mi_int(mi, ".attention.value_length")
    if (klen is None or vlen is None) and heads:                   # derive head dim from embedding
        emb = _mi_int(mi, ".embedding_length")
        if emb:
            klen = vlen = emb // heads
    if not (layers and kv_heads and klen and vlen):
        return 0
    return layers * kv_heads * (klen + vlen) * 2


class OllamaProvider:
    def __init__(self, name: str = "ollama", host: str | None = None, api_key: str | None = None):
        self.name = name
        self.host = (host or os.environ.get("OLLAMA_API_BASE")
                     or os.environ.get("OLLAMA_HOST") or LOCAL_DEFAULT).rstrip("/")
        self.api_key = api_key
        self._ctx_cache: dict[str, int] = {}   # model -> effective num_ctx
        self._show_cache: dict[str, dict] = {}  # model -> /api/show payload

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    # --- context window ------------------------------------------------------
    def _show(self, model: str) -> dict:
        if model not in self._show_cache:
            try:
                self._show_cache[model] = post_json(
                    f"{self.host}/api/show", {"model": model},
                    headers=self._headers(), timeout=10, provider=self.name)
            except Exception:
                self._show_cache[model] = {}
        return self._show_cache[model]

    def _weight_bytes(self, model: str) -> int:
        try:
            data = get_json(f"{self.host}/api/tags", headers=self._headers(), provider=self.name)
            for m in data.get("models", []):
                if m.get("name") == model and isinstance(m.get("size"), int):
                    return m["size"]
        except Exception:
            pass
        return 0

    def _compute_ctx(self, model: str) -> int:
        """The largest window this machine can run comfortably for this model:
        min(model's trained max, what fits in RAM after weights + a headroom
        reserve, using ~75% of the rest for the KV cache). Rounds to a clean
        multiple. Falls back to CTX_FALLBACK if RAM or arch can't be read."""
        mi = self._show(model).get("model_info") or {}
        trained = _mi_int(mi, ".context_length") or 0
        per_tok = _kv_bytes_per_token(mi)
        ram = _total_ram_bytes()
        weights = self._weight_bytes(model)
        if not (trained and per_tok and ram and weights):
            return min(trained, CTX_FALLBACK) if trained else CTX_FALLBACK
        avail = max(0.0, ram - weights - KV_RESERVE_BYTES) * KV_USE_FRACTION
        ram_ctx = int(avail / per_tok)
        eff = min(trained, ram_ctx)
        eff = max(CTX_FLOOR, (eff // CTX_ROUND) * CTX_ROUND)
        return min(eff, trained)

    def context_window(self, model: str) -> int:
        """The window 2B pins (via num_ctx) and budgets for. TWOB_CONTEXT_TOKENS
        overrides; cloud isn't pinned; otherwise it's computed per machine+model
        so we run as large as the box handles comfortably, no more. Cached."""
        env = os.environ.get("TWOB_CONTEXT_TOKENS")
        if env and env.isdigit():
            return int(env)
        if self.api_key is not None:            # cloud
            return CLOUD_CTX
        if model not in self._ctx_cache:
            self._ctx_cache[model] = self._compute_ctx(model)
        return self._ctx_cache[model]

    def _options(self, model: str) -> dict:
        """Runtime options. Conservative sampling (low temperature + a mild
        repeat penalty) steadies small-model tool selection and curbs
        degenerate loops — a host-side reliability lever, no schema change. We
        deliberately do NOT pin a seed: an identical seed would make a repair
        retry regenerate the same malformed call verbatim. Locally we also pin
        num_ctx to context_window so the model runs at the size 2B budgets for
        (Ollama otherwise defaults to a small ~4k window regardless of the
        model's trained max)."""
        opts = {"temperature": 0.2, "repeat_penalty": 1.1}
        # Opt-in reproducible sampling for the eval harness only (P9 multi-seed variance):
        # when TWOB_SAMPLING_SEED is set, pin that seed so a run is reproducible and distinct
        # seeds measure across-seed variance. Unset in production — see the docstring on why
        # a fixed seed would make a malformed-call repair regenerate the same bad call.
        seed = os.environ.get("TWOB_SAMPLING_SEED")
        if seed:
            try:
                opts["seed"] = int(seed)
            except ValueError:
                pass
        if self.api_key is None:
            opts["num_ctx"] = self.context_window(model)
        return opts

    def is_available(self) -> bool:
        if self.api_key is not None and not self.api_key:
            return False
        try:
            self.list_models()
            return True
        except Exception:
            return False

    def list_models(self) -> list[str]:
        data = get_json(f"{self.host}/api/tags", headers=self._headers(), provider=self.name)
        return [m["name"] for m in data.get("models", [])]

    def _messages(self, conv: Conversation) -> list[dict]:
        out = [{"role": "system", "content": conv.system_prompt}]
        for m in conv.messages:
            if m.tool_results:
                for r in m.tool_results:
                    out.append({"role": "tool", "content": r.content})
                continue
            if m.role == Role.ASSISTANT:
                entry: dict = {"role": "assistant", "content": m.text or ""}
                if m.tool_calls:
                    entry["tool_calls"] = [
                        {"function": {"name": tc.name, "arguments": tc.arguments}} for tc in m.tool_calls
                    ]
                out.append(entry)
            else:
                out.append({"role": "user", "content": m.text or ""})
        return out

    def send(self, conversation: Conversation, model: str, tools: tuple[ToolSpec, ...]) -> ProviderResponse:
        payload = {
            "model": model,
            "messages": self._messages(conversation),
            "tools": to_openai(tools),
            "stream": False,
            "options": self._options(model),
            "keep_alive": KEEP_ALIVE,
        }
        raw = post_json(f"{self.host}/api/chat", payload, headers=self._headers(),
                        provider=self.name)
        msg = raw.get("message", {})
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c["function"]
            args = _parse_args(fn["arguments"])
            calls.append(ToolCall.new(name=fn["name"], arguments=args))
        text = (msg.get("content") or "").strip()
        if not calls and text:
            recovered = recover_toolcalls(text, [t.name for t in tools])
            calls = [ToolCall.new(name=n, arguments=a) for n, a in recovered]
        thinking = (msg.get("thinking") or "").strip()
        return ProviderResponse(
            message=Message.assistant(text=text or None, thinking=thinking or None, tool_calls=calls),
            raw=raw,
            done_reason=raw.get("done_reason"),
            prompt_tokens=raw.get("prompt_eval_count"),
        )

    def stream(self, conversation: Conversation, model: str, tools: tuple[ToolSpec, ...],
               on_text: Callable[[str], None], *, cancel=None) -> ProviderResponse:
        payload = {
            "model": model,
            "messages": self._messages(conversation),
            "tools": to_openai(tools),
            "stream": True,
            "options": self._options(model),
            "keep_alive": KEEP_ALIVE,
        }
        content, thinking, calls = [], [], []
        done_reason = prompt_tokens = None
        for line in post_stream(f"{self.host}/api/chat", payload, headers=self._headers(),
                                provider=self.name, cancel=cancel):
            line = line.strip()
            if not line:
                continue
            obj = _json.loads(line)
            m = obj.get("message", {})
            if m.get("content"):
                content.append(m["content"])
                on_text(m["content"])
            if m.get("thinking"):
                thinking.append(m["thinking"])
            for c in m.get("tool_calls") or []:
                fn = c["function"]
                args = _parse_args(fn["arguments"])
                calls.append(ToolCall.new(name=fn["name"], arguments=args))
            if obj.get("done"):
                done_reason = obj.get("done_reason")
                prompt_tokens = obj.get("prompt_eval_count")
                break
        text = "".join(content).strip()
        if not calls and text:
            recovered = recover_toolcalls(text, [t.name for t in tools])
            calls = [ToolCall.new(name=n, arguments=a) for n, a in recovered]
        think = "".join(thinking).strip()
        return ProviderResponse(
            message=Message.assistant(text=text or None, thinking=think or None, tool_calls=calls),
            raw={},
            done_reason=done_reason,
            prompt_tokens=prompt_tokens,
        )


    def perf(self, model: str) -> str:
        """Live footprint of a loaded local model via /api/ps: total RAM and the
        GPU/CPU split — the local-model equivalent of a token counter. '' if the
        model isn't loaded yet or ps is unavailable."""
        try:
            data = get_json(f"{self.host}/api/ps", headers=self._headers(), provider=self.name)
        except Exception:
            return ""
        for m in data.get("models", []):
            if model in (m.get("name"), m.get("model")):
                total = m.get("size", 0) or 0
                vram = m.get("size_vram", 0) or 0
                if total <= 0:
                    return ""
                gb = total / 1e9
                if vram >= total:
                    proc = "100% GPU"
                elif vram <= 0:
                    proc = "100% CPU"
                else:
                    gpu = round(vram / total * 100)
                    proc = f"{gpu}% GPU / {100 - gpu}% CPU"
                return f"{gb:.1f}GB · {proc}"
        return ""


def local() -> OllamaProvider:
    return OllamaProvider(name="ollama")


def cloud() -> OllamaProvider | None:
    key = os.environ.get("OLLAMA_API_KEY")
    if not key:
        return None
    return OllamaProvider(name="ollama-cloud", host=CLOUD_HOST, api_key=key)
