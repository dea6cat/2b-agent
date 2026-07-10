# Reasoning/Thinking Control (`/think`) — Design

**Status:** approved for implementation (brainstormed 2026-07-10).

## Goal

A host-side lever over model reasoning — turn it **off** for speed, **on**, or set a **level** —
for the providers where it is clean and safe: **Ollama** (2B's local core) and **Google** now.
Anthropic and OpenAI-compatible providers are **deferred** (see §7) with an honest note. The
frozen five-tool schema is untouched: reasoning is a **provider-call parameter**, not a tool the
model sees.

## Why control, not "turn on"

The local reasoning models 2B targets — `qwen3`, `qwen3.5`, `gpt-oss` — already reason **by
default**; Ollama emits their thinking and 2B already captures it into `Message.thinking`. So the
real, local-first value is a *lever*: turn thinking **off** to kill the multi-minute stalls on slow
hardware, or dial a **level**, rather than "enable something that's off." Cloud reasoning
enablement (Anthropic/OpenAI-compat) is the lower-value, higher-cost part and is deferred.

This is control-only: **reasoning output display is out of scope** (§6). 2B keeps capturing
thinking exactly as it does today; it does not stream it live, and for Google it does not read the
thought parts back at all — it only *sets* the budget.

## Control model

### Levels (unified vocabulary)

`off · on · low · medium · high`, plus the internal sentinel `None` meaning "no user preference —
use the provider's sensible (capped) default."

### Precedence (highest wins)

1. Session override — `/think <level>` (this session only, like `/mode`)
2. Environment — `TWOB_THINK=<level>` (persistent)
3. Default policy — `None` (provider decides; see per-provider mapping)

This mirrors the existing `_continuity_effective` precedent (`orchestrator.py:438`): a user
override wins, else the provider default applies.

### Default policy (the "on for capable, capped, off otherwise" decision)

The host passes `reasoning=None` when there is no override. Each wired provider interprets `None`
as its **sensible, latency-capped default**:

- **Ollama** — `None` → send **no** `think` param → the model's own default applies. A
  reasoning-capable model (`qwen3`, `gpt-oss`, …) thinks; a non-thinking model does not. This
  realizes "on for capable, off for others" with zero host logic and no risk of erroring a
  non-capable model.
- **Google** — `None` → send a **capped** default `thinkingBudget` (the `medium` tier, §per-provider)
  rather than Gemini 2.5's unbounded *dynamic* thinking. This is the latency cap from the approved
  design; `/think high` raises it, `/think off` removes it.

So under the default policy, Ollama sends nothing and Google sends a bounded budget. The reasoning
param is otherwise only sent when the user explicitly sets a level.

## Per-provider mapping

`provider.stream(...)` gains a keyword arg `reasoning: str | None = None`. Each provider maps it to
its native wire field; a provider we did not wire simply ignores the arg.

### Ollama (`providers/ollama.py`)

Ollama's `/api/chat` takes a top-level `think` field. Capability is read from `/api/show`'s
`capabilities` array (contains `"thinking"` for models that support it), cached per model.

| `reasoning` | payload |
|---|---|
| `None` | omit `think` (model default) |
| `"off"` | `think: false` |
| `"on"` | `think: true` |
| `"low"/"medium"/"high"` | `think: true` for boolean models; native string `think: "<level>"` for `gpt-oss` (accepts string levels) |

**Capability guard:** if the model is **not** thinking-capable, never add a `think` key (Ollama
returns an error otherwise). An explicit `off`/`on`/level on a non-capable model is a silent no-op
at the wire level; the `/think` command surfaces the "not supported" message (below). Applies to
both local Ollama and Ollama Cloud (`api_key` set).

### Google (`providers/google.py`)

Add `generationConfig.thinkingConfig.thinkingBudget` (integer tokens) to the payload. Do **not**
set `includeThoughts` (display out of scope). Budgets are clamped to the model's documented bounds.

| `reasoning` | `thinkingBudget` |
|---|---|
| `None` | `MED` (capped default) |
| `"off"` | `0` (Flash/Flash-Lite); for models that cannot fully disable (2.5 Pro), the model minimum, and we note the limitation |
| `"low"` | `LOW` |
| `"medium"` / `"on"` | `MED` |
| `"high"` | `HIGH` |

Tiers (starting point, refined in the plan; clamped per model): `LOW≈2048`, `MED≈8192`,
`HIGH≈24576`. Never `-1` (dynamic/uncapped).

## Surface

- **`/think`** (no arg) → print the current effective level and whether the active model supports
  reasoning (via the provider capability check).
- **`/think off|on|low|medium|high`** → set `session.think` for this session. Not persisted.
- **`/think <garbage>`** → error message listing valid levels.
- **`TWOB_THINK=off|on|low|medium|high`** → persistent override; invalid values ignored.
- New `Session` field: `think: str | None = None` (None = default policy). Registered as a
  `@command("think")` in `commands.py`, alongside `/mode` and `/continuity`.

## Wiring

1. `Session.think` field (session.py).
2. `_reasoning_effective(session) -> str | None` in `orchestrator.py` (next to
   `_continuity_effective`): returns `session.think` if set, else a valid `TWOB_THINK`, else `None`.
   Pure precedence — no provider/model needed.
3. `stream_with_retry(provider, conversation, model, tools, on_text, *, retries=3, cancel=None,
   reasoning=None)` (`providers/base.py:181`) — forward `reasoning` to `provider.stream`.
4. The two foreground call sites (`orchestrator.py:1553`, `:1785`) pass
   `reasoning=_reasoning_effective(session)`.
5. Sub-agent call sites (`subagents.py:37`, `:207`) pass **nothing** → `reasoning=None` (default;
   sub-agents stay simple/fast). Noted, not a gap.
6. `provider.stream` signatures gain `reasoning: str | None = None`. Ollama and Google map it;
   Anthropic and OpenAI-compat accept-and-ignore it (so the call site is uniform).
7. `send()` (non-streaming; used only for compaction) is untouched — it ignores reasoning.
8. Provider capability probe for the `/think` status message: a `supports_reasoning(model) -> bool`
   method. Ollama implements it via `/api/show`; Google returns True for `gemini-2.5*`; the
   deferred providers return False.

## Out of scope (this phase)

- **Live thinking display** — no streaming thinking panel; capture behavior is unchanged. A later
  phase can add a TUI panel and, for Google, `includeThoughts` + thought-part parsing.
- **Persisting `/think`** to prefs (env var is the persistent form).
- **Anthropic & OpenAI-compat reasoning** — see §7.

## §7 Deferred providers (documented honestly)

- **Anthropic** — enabling `thinking:{type:"enabled",budget_tokens:N}` **with tool use** requires
  preserving the *signed* thinking block and replaying it in every subsequent request, or the API
  returns 400. That needs a signature field on `Message` and reconstruction in the request builder
  — a materially larger change. Anthropic reasoning stays **off** this phase.
- **OpenAI-compat** (OpenRouter, DeepSeek, Mistral, NVIDIA, Cerebras) — the reasoning knob is
  heterogeneous (`reasoning_effort` vs `reasoning:{}`), and some endpoints **400 on an unknown
  param**. Sending it safely means per-endpoint gating. Reasoning stays **off** this phase.

Both accept-and-ignore the `reasoning` arg so the wiring is uniform and a future phase only touches
those two files.

## Testing (unit; no live API)

- **Ollama mapping** — monkeypatch the streaming transport to capture the payload; assert:
  `None`→no `think` key; `off`→`think:false`; `on`→`think:true`; `gpt-oss` level→string `think`;
  non-capable model→no `think` key regardless of level.
- **Google mapping** — assert `thinkingBudget` for each level, `None`→MED, `off`→0/min, clamping.
- **Precedence** — `_reasoning_effective`: session > `TWOB_THINK` > None; invalid env ignored.
- **`/think` command** — no-arg shows status; valid level sets `session.think`; invalid rejected;
  non-capable model gets the "not supported" message.
- **`stream_with_retry`** — a fake provider records that `reasoning` is forwarded to `stream`.

## Non-goals / invariants

- Frozen five-tool schema unchanged (reasoning is a provider param, not a tool).
- Host-side only; no new model-facing surface.
- `verify.py`/tool loop untouched.
- Relates to the deferred note `reasoning-mode-deferred` and `2b-design-philosophy`
  (frozen schema, host-side, phased + honest).
