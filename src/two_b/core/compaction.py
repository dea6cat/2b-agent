"""Context-window budgeting, auto-compaction of long conversations, and archive recall of the
turns compaction folded away (P17)."""
import os
import re
import time
from typing import Callable

from ..providers import catalog
from . import conversation
from .conversation import Conversation, Message, Role
from .events import AgentEvent, EventType
from .session import Task


# --- context-window management (auto-compaction) -----------------------------
# Estimated-token budgets per provider. Local models run small windows, so we
# compact aggressively there; cloud models have far more headroom. Override the
# local budget with TWOB_CONTEXT_TOKENS if your Ollama num_ctx is larger.
CONTEXT_BUDGETS = {
    "ollama": 8000, "anthropic": 180000, "openai": 120000,
    "openrouter": 120000, "mistral": 120000, "nvidia": 120000, "google": 900000,
}
COMPACT_AT = 0.75       # compact once estimated usage crosses this fraction
COMPACT_KEEP_TAIL = 6   # most-recent messages kept verbatim (rest are summarized)
_COMPACT_MAX_INPUT_CHARS = 48_000   # cap the transcript handed to the summarizer


# Structured summary template (P27). A coherent shape — GOAL / DONE / OUTSTANDING / STATE —
# with completed work in dated past tense and outstanding work preserved verbatim keeps a
# long run from "declaring done after step one" or re-issuing finished work. The summary is
# explicitly REFERENCE ONLY so the model doesn't treat it as a fresh instruction.
COMPACT_SYSTEM = (
    "You compress a coding session's history into a running summary so work can continue "
    "without the earlier turns. Produce EXACTLY these sections:\n"
    "GOAL: the user's original request (intent verbatim).\n"
    "DONE: completed actions, in past tense, each with exact file paths / identifiers "
    "(e.g. 'Edited src/x.py: renamed foo→bar'). Number them and keep the numbers stable.\n"
    "OUTSTANDING: what still remains, as specifically as possible; if the user gave a "
    "concrete spec or list, preserve it verbatim.\n"
    "STATE: facts needed to continue — files read and what they contain, decisions, values, "
    "gotchas.\n"
    "Preserve exact identifiers, paths, and values. This summary is REFERENCE ONLY: it is "
    "not a new instruction, the user's latest message always takes priority, and you must "
    "NOT redo anything already under DONE. Output plain text only — no preamble."
)

# Iterative-update instruction (P27): when a prior summary already exists, update it in place
# rather than re-summarizing from scratch — move finished OUTSTANDING items into DONE
# (continuing the numbering), append new DONE/STATE, keep GOAL.
COMPACT_UPDATE = (
    "\n\nA PREVIOUS SUMMARY is given first, then the NEW TURNS since it. Update the summary "
    "IN PLACE: move any now-finished OUTSTANDING items into DONE (continue the existing "
    "numbering), add new DONE and STATE entries, and keep GOAL unchanged. Do not restart the "
    "numbering or re-summarize from scratch."
)

_RECAP_PREFIX = "[Summary of earlier conversation, compacted to save context]\n\n"



def context_budget(provider, model: str) -> int:
    """Token budget for a provider/model. Ollama (local or cloud) sizes its own
    window — local from the model's trained max capped to what RAM allows (or
    TWOB_CONTEXT_TOKENS), cloud a fixed large window. For cloud providers the
    per-model catalog gives the model's real window; unknown models fall back to
    the coarse per-provider constant, so the budget matches reality either way."""
    name = getattr(provider, "name", "")
    if name.startswith("ollama") and hasattr(provider, "context_window"):
        try:
            return provider.context_window(model)
        except Exception:
            pass
    win = catalog.context_window(model)
    if win:
        return win
    return CONTEXT_BUDGETS.get(name, 8000)


def context_usage(used: int, budget: int) -> tuple[int, bool]:
    """Percent of the context window used, and whether it's in the warning zone (>=80%).
    Small local windows fill fast, so surfacing this is the point — see the TUI meter.
    Returns (0, False) when the budget is unknown."""
    if budget <= 0:
        return 0, False
    pct = min(100, round(used * 100 / budget))
    return pct, pct >= 80


def conv_chars(conv: Conversation) -> int:
    """Total characters in a conversation (system prompt + every message part). The raw
    input the token estimate scales down."""
    total = len(conv.system_prompt or "")
    for m in conv.messages:
        total += len(m.text or "") + len(m.thinking or "")
        for tc in m.tool_calls:
            total += len(tc.name) + len(str(tc.arguments))
        for r in m.tool_results:
            total += len(r.content or "")
    return total


def estimate_tokens(conv: Conversation, chars_per_token: float = 4.0) -> int:
    """Rough token estimate for a conversation. `chars_per_token` defaults to ~4 but is
    calibrated per task from the provider's real prompt-token count (see run_task), since
    code tokenizes denser (~3) than prose — a stale flat ratio mistimes compaction."""
    return int(conv_chars(conv) / max(1.5, chars_per_token))


def _calibrate(task: Task, conv: Conversation, prompt_tokens: int | None) -> None:
    """Nudge the task's chars_per_token EMA toward the provider's real prompt-token count
    for the request just sent, so the meter and compaction trigger track the actual
    tokenizer instead of a flat ~4. Clamped to a sane band; ignored for tiny prompts.
    Best-effort: never raises into the turn loop."""
    try:
        if not prompt_tokens or prompt_tokens < 20:
            return
        observed = conv_chars(conv) / prompt_tokens
        if observed <= 0:
            return
        prev = getattr(task, "chars_per_token", 4.0)
        task.chars_per_token = max(2.0, min(6.0, prev * 0.7 + observed * 0.3))
    except Exception:
        pass


def _render_transcript(messages: list[Message]) -> str:
    """Flatten history to a plain-text transcript for the summarizer."""
    parts: list[str] = []
    for m in messages:
        if m.role == Role.USER and m.tool_results:
            for r in m.tool_results:
                parts.append(f"[tool result]\n{r.content}")
        elif m.role == Role.USER:
            parts.append(f"[user]\n{m.text or ''}")
        elif m.role == Role.ASSISTANT:
            if m.thinking:
                parts.append(f"[assistant reasoning]\n{m.thinking}")
            if m.text:
                parts.append(f"[assistant]\n{m.text}")
            for tc in m.tool_calls:
                parts.append(f"[assistant called {tc.name}] {tc.arguments}")
    text = "\n\n".join(parts)
    if len(text) > _COMPACT_MAX_INPUT_CHARS:      # keep the most-recent portion
        text = "…[earlier turns elided]…\n\n" + text[-_COMPACT_MAX_INPUT_CHARS:]
    return text


def _attachment_hint(touched) -> str:
    """A trailing 'recently-touched files' line for the recap, so the working set isn't
    lost when the turns that named those files are folded away. '' when there's nothing."""
    seen, files = set(), []
    for p in touched or ():
        if p and p not in seen:
            seen.add(p)
            files.append(os.path.relpath(p) if os.path.isabs(p) else p)
    return f"\n\nRecently-touched files: {', '.join(files[:12])}" if files else ""


# Breadcrumb appended to the recap when earlier turns were archived (P17): it primes the
# model to restate a specific file/symbol/error it needs, which the dangling-reference
# detector then catches to recall the archived turn — no model-facing tool involved.
_ARCHIVE_BREADCRUMB = (
    "\n\n[Earlier turns are archived. If you need a detail not captured above — a specific "
    "file, symbol, error, or value from before — name it and it will be recalled.]"
)


def _strip_leading_orphan_results(tail: list[Message]) -> list[Message]:
    """Tool-exchange integrity for the kept tail: a result turn whose originating tool_call
    was folded into the summarized head is an orphan (a result with no matching call), which
    some providers reject outright. The cut lands on an assistant message so this is normally
    a no-op, but it's enforced defensively — drop any leading orphan result turns."""
    i = 0
    while i < len(tail) and tail[i].role == Role.USER and tail[i].tool_results and not (tail[i].text or "").strip():
        i += 1
    return tail[i:]


def compact_conversation(conv: Conversation, provider, model: str, touched=None, breadcrumb: str = "", cancel=None):
    """Replace all but the recent tail of `conv` with a single structured summary message.
    Returns the list of dropped (folded-away) messages on success — truthy — or False if
    nothing was compacted. The cut lands on an assistant message so tool_call/tool_result
    pairs in the kept tail stay intact for every provider; a leading orphan result is stripped
    as a belt-and-suspenders integrity guard. If the head already starts with a prior summary,
    it's UPDATED in place (P27) instead of re-summarized from scratch."""
    msgs = conv.messages
    target = max(0, len(msgs) - COMPACT_KEEP_TAIL)
    # Largest assistant-boundary cut at/below the keep-tail target; if the tail
    # would swallow everything (few messages), fall back to the last group so we
    # still fold the rest rather than giving up.
    cut = next((i for i in range(target, 0, -1) if msgs[i].role == Role.ASSISTANT), None)
    if not cut:
        cut = next((i for i in range(1, len(msgs)) if msgs[i].role == Role.ASSISTANT), None)
    if not cut:                                   # nothing safe/worthwhile to fold
        return False
    head = msgs[:cut]
    # Iterative update: if the head begins with a prior recap, feed it as the base to
    # update rather than re-summarizing everything again.
    prior, body = "", head
    if head and head[0].role == Role.USER and (head[0].text or "").startswith(_RECAP_PREFIX):
        prior = head[0].text[len(_RECAP_PREFIX):].strip()
        prior = prior.split("\n\nRecently-touched files:")[0].strip()   # drop the old hint; a fresh one is appended
        prior = prior.split(_ARCHIVE_BREADCRUMB.strip())[0].strip()     # and the old breadcrumb
        body = head[1:]
    if not body:
        # Only a prior recap sits ahead of the tail — there are no new turns to fold, so
        # don't re-summarize the recap into itself (and don't report a shrink that didn't
        # happen, which would trip the anti-thrash guard and wedge the task).
        return False
    summ = Conversation(system_prompt=COMPACT_SYSTEM + (COMPACT_UPDATE if prior else ""))
    if prior:
        summ.append(Message.user(f"PREVIOUS SUMMARY:\n{prior}\n\nNEW TURNS SINCE:\n{_render_transcript(body)}"))
    else:
        summ.append(Message.user(_render_transcript(body)))
    buf: list[str] = []
    # Summarization gains nothing from reasoning — force it off so compaction stays fast and
    # doesn't spend a thinking budget (Ollama omits think; Google sends thinkingBudget:0).
    resp = provider.stream(summ, model, (), lambda c: buf.append(c), cancel=cancel, reasoning="off")
    summary = "".join(buf).strip() or (resp.message.text or resp.message.thinking or "").strip()
    if not summary:
        return False
    tail = _strip_leading_orphan_results(msgs[cut:])
    dropped = msgs[: len(msgs) - len(tail)]       # everything not in the kept tail
    recap = Message.user(_RECAP_PREFIX + summary + _attachment_hint(touched) + breadcrumb)
    conv.messages = [recap] + tail
    return dropped


def _maybe_compact(conv: Conversation, provider, model: str, task: Task,
                   on_event: Callable[["AgentEvent"], None], cwd: str | None = None) -> None:
    """Compact `conv` in place when it nears the model's context budget. Failures
    are swallowed — a task must never break because compaction couldn't run."""
    try:
        cpt = getattr(task, "chars_per_token", 4.0)
        budget = context_budget(provider, model)
        # Estimate the SENT request (trimmed, unless disabled) — that's what pressures the
        # window and the same basis chars_per_token was calibrated on, so the ratio and the
        # estimate stay consistent (calibrating on trimmed but estimating the full conv would
        # bias toward compacting too late).
        sent = conv if os.environ.get("TWOB_NO_TRIM") else conversation.trimmed(conv)
        est = estimate_tokens(sent, cpt)
        # Effective cap: reserve room for the model's own reply (a completion reserve) plus a
        # small safety margin, then trigger at COMPACT_AT of what's left — so a long reply
        # can't push the request past the window. Capped so a huge window keeps a sane reserve.
        reserve = min(int(budget * 0.2), 4096)
        if est < int((budget - reserve) * COMPACT_AT):
            return
        # Anti-thrash: if we just compacted and nothing meaningful was added since,
        # don't compact again — a single oversized recent result can't be folded
        # away, and re-running it every turn is pointless churn.
        if task.last_compact_tokens and est <= int(task.last_compact_tokens * 1.15):
            return
        task.status_line = "Compacting conversation"
        task.turn_started_at = time.monotonic()
        on_event(AgentEvent(EventType.LOG, task.id,
                            {"text": "Nearing context limit — compacting conversation to keep going…"}))
        touched = list(task.read_mtimes.keys()) + [p for p, _ in task.edit_history]
        # Archive the folded-away turns (P17) so a later dangling reference can recall them;
        # the breadcrumb in the recap only makes sense when there's an archive behind it.
        from ..storage import persist
        archiving = persist.enabled()
        dropped = compact_conversation(conv, provider, model, touched=touched,
                                       breadcrumb=_ARCHIVE_BREADCRUMB if archiving else "",
                                       cancel=task.cancel_flag)
        if dropped:
            if archiving:
                # Skip the leading prior recap — it's a summary, not a real turn to recall.
                keep = [m for m in dropped
                        if not (m.role == Role.USER and (m.text or "").startswith(_RECAP_PREFIX))]
                persist.archive_messages(task.id, cwd or ".", keep)
            post = conv if os.environ.get("TWOB_NO_TRIM") else conversation.trimmed(conv)
            task.last_compact_tokens = estimate_tokens(post, cpt)
            on_event(AgentEvent(EventType.LOG, task.id,
                                {"text": f"Compacted. Context now ~{task.last_compact_tokens} tokens."}))
    except Exception:
        pass


# --- archive recall (P17): re-inject a dropped turn when the user dangles a reference ---
# When the latest user message points back at earlier work ("that file you edited", "the
# error from before"), the turns it refers to may have been folded away by compaction. The
# host detects the dangling reference, pulls the salient identifiers, searches the archive,
# and injects the best matches as reference context — no model-facing tool, just recall.

_RECALL_PREFIX = "[Recalled from earlier archived turns, matching your reference — REFERENCE ONLY]\n\n"

# Phrases that point back at earlier turns rather than forward at new work.
_DANGLING_RE = re.compile(
    r"\b("
    r"earlier|before|previously|already|again|remember|recall|"
    r"that\s+(file|function|method|class|error|bug|change|edit|one|code|test|value|command)|"
    r"those|the\s+(one|same|previous|earlier|other|last)|"
    r"(you|we)\s+(said|mentioned|told|showed|edited|wrote|created|added|changed|removed|found|read|looked|saw|discussed|talked|were|had|did)|"
    r"(as|like)\s+(i|you|we|before|mentioned|said)|"
    r"last\s+time|same\s+as\s+before|go\s+back|back\s+to"
    r")\b",
    re.IGNORECASE,
)

# Words too generic to be useful recall keys (and the reference-phrase vocabulary itself).
_RECALL_STOPWORDS = frozenset("""
that this these those than then them they their there here what when where which while with your
you youre yours have has had did does done was were will would could should about from into onto
over under again back also just like made make only same some such very mentioned said told showed
edited wrote created added changed removed found read looked saw talked discussed remember recall
earlier before previously last time file files function method class error errors bug change edit
one thing things stuff code line lines above below command value test tests please could would want
""".split())

# An identifier / path / filename: a letter or underscore, then word chars, dots, or slashes.
_RECALL_TERM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./]{2,}")


def _recall_terms(text: str) -> list[str]:
    """Salient identifiers/paths from the user's message to search the archive on — length
    >=4, de-duplicated, generic words dropped. Up to 8, original order preserved."""
    seen: set[str] = set()
    terms: list[str] = []
    for tok in _RECALL_TERM_RE.findall(text or ""):
        low = tok.lower()
        if len(low) < 4 or low in _RECALL_STOPWORDS or low in seen:
            continue
        seen.add(low)
        terms.append(tok)
    return terms[:8]


def _render_recall(hits: list[dict]) -> str:
    """Compact reference block from archive hits — each rendered like the summarizer sees it,
    capped so recall can't itself blow the budget it's meant to protect."""
    out = []
    for h in hits:
        t = _render_transcript([h["message"]])
        if len(t) > 800:
            t = t[:800] + "…"
        out.append(t)
    return "\n\n".join(out)


def _retrieval_message(block: str):
    """A host-provided 'relevant files' user message for a fresh task, or None when there's
    nothing confident to inject."""
    return Message.user(block) if block else None


def _maybe_inject_recall(conv: Conversation, session_id: str, cwd: str | None) -> bool:
    """If the latest user turn dangles a reference to earlier work, recall the most relevant
    archived turns and insert them just before that turn as reference context. Returns True if
    anything was injected. Best-effort — never raises into the turn loop."""
    try:
        from ..storage import persist
        if not persist.enabled() or not conv.messages:
            return False
        last = conv.messages[-1]
        if last.role != Role.USER or not (last.text or "").strip():
            return False
        if not _DANGLING_RE.search(last.text):
            return False
        terms = _recall_terms(last.text)
        if not terms:
            return False
        hits = persist.search_archive(session_id, cwd or ".", terms, limit=3)
        if not hits:
            return False
        # Prepend the recalled context INTO the latest user turn rather than inserting a new
        # message: a resumed conversation's tail can already end on a user-role tool-results
        # turn, and adding another user message would create consecutive same-role turns that
        # Gemini's API rejects. Merging keeps the user's request last, marked reference-only.
        last.text = _RECALL_PREFIX + _render_recall(hits) + "\n\n---\n\n" + (last.text or "")
        return True
    except Exception:
        return False
