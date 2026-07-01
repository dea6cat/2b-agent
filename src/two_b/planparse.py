"""Extract a plan checklist from the model's own text — no new tool.

The model is asked (via one appended system-prompt sentence, see PLAN_PROMPT)
to write a numbered plan as plain text before its first tool call. We parse
that text locally into PlanStep objects and infer active/done state from the
tool calls that follow. This is entirely host-side and best-effort: if no
plan-shaped text is found, the checklist simply isn't shown (clean fallback),
and a wrong active-step guess is cosmetic only — it never affects the actual
model loop or tool execution.
"""
import re

from .session import PlanStep

# Appended to the base system prompt when checklist rendering is enabled.
PLAN_PROMPT = (
    " Before making any tool calls, first write a short numbered plan (e.g. "
    "'1. ... 2. ...') of the steps you intend to take, as plain text; then "
    "proceed with your tool calls."
)

MIN_STEPS_TO_ACCEPT = 2
MAX_STEP_CHARS = 100

# Tried in order per line; the LAST capture group is always the step text.
_PATTERNS = [
    re.compile(r"^\s*\*\*(\d+)[.\)]\*\*\s+(.*\S)\s*$"),  # **1.** foo  (bold markdown numbering)
    re.compile(r"^\s*(\d+)[.\)]\s+(.*\S)\s*$"),           # 1. foo / 1) foo
    re.compile(r"^\s*[-*•]\s+(.*\S)\s*$"),            # - foo / * foo / • foo
]


def extract_plan(assistant_text: str) -> list[PlanStep] | None:
    """Best-effort parse of a numbered/bulleted plan out of the model's text.
    Returns None if fewer than MIN_STEPS_TO_ACCEPT plan-shaped lines are found,
    so the caller can fall back to the plain status line."""
    if not assistant_text:
        return None
    steps: list[PlanStep] = []
    for line in assistant_text.splitlines():
        for pat in _PATTERNS:
            m = pat.match(line)
            if m:
                text = m.group(m.lastindex).strip().rstrip(".")
                if text:
                    steps.append(PlanStep(text=text[:MAX_STEP_CHARS]))
                break
    return steps if len(steps) >= MIN_STEPS_TO_ACCEPT else None


_WORD_RE = re.compile(r"[a-z0-9]{4,}")


def _overlap_score(step_text: str, tool_text: str) -> int:
    step_words = set(_WORD_RE.findall(step_text.lower()))
    tool_words = set(_WORD_RE.findall(tool_text.lower()))
    return len(step_words & tool_words)


def infer_active_step(steps: list[PlanStep], tool_name: str, tool_args: dict) -> None:
    """Mutate `steps` in place: mark the previously active step done, then pick
    a new active step by naive keyword overlap between the tool call's string
    args and each step's text. Falls back to advancing to the next pending step
    when nothing matches. Purely for display."""
    if not steps:
        return
    for s in steps:
        if s.status == "active":
            s.status = "done"

    candidate = " ".join(str(v) for v in tool_args.values() if isinstance(v, str))
    best, best_score = None, 0
    for s in steps:
        if s.status == "done":
            continue
        score = _overlap_score(s.text, candidate)
        if score > best_score:
            best, best_score = s, score
    if best is None:
        best = next((s for s in steps if s.status == "pending"), None)
    if best is not None:
        best.status = "active"


def finalize_steps(steps: list[PlanStep]) -> None:
    """On task completion, mark any remaining active/pending steps done so the
    checklist doesn't freeze mid-way when the model wraps up early."""
    for s in steps:
        if s.status != "done":
            s.status = "done"
