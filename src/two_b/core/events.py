"""Events a running task emits to the UI thread, and the readable one-line reason a failure carries."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..providers.base import ProviderError


class EventType(Enum):
    TURN_START = "turn_start"
    ASSISTANT_DELTA = "assistant_delta"    # a streamed chunk of the reply
    ASSISTANT_TEXT = "assistant_text"      # (legacy; kept for non-stream callers)
    THINKING_DELTA = "thinking_delta"      # a streamed chunk of the model's reasoning
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_RESULT = "tool_call_result"
    LOG = "log"                # captured tool stdout, to print to scrollback
    TASK_DONE = "task_done"
    TASK_ERROR = "task_error"


@dataclass
class AgentEvent:
    type: EventType
    task_id: str
    payload: dict[str, Any] = field(default_factory=dict)



def _classify_exc(e: BaseException) -> str:
    """A readable one-line reason for an otherwise-opaque exception. ProviderError
    already carries a '[provider] message'; for everything else, name the type so a
    blank-message exception (e.g. KeyError('path')) never surfaces as empty output."""
    if isinstance(e, ProviderError):
        return str(e)
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__
