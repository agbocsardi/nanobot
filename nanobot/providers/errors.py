"""Provider error classification shared by retries and request coordinators."""

from typing import Any


def is_context_overflow(response: Any) -> bool:
    """Recognize explicit context rejection, never a generic transport failure."""
    if getattr(response, "finish_reason", None) != "error":
        return False
    kind = str(getattr(response, "error_kind", "") or "").lower()
    if kind in {"context_length_exceeded", "context_window_exceeded", "context_overflow"}:
        return True
    if kind in {"timeout", "connection", "transport"}:
        return False
    text = str(getattr(response, "content", "") or "").lower()
    return any(marker in text for marker in (
        "context_length_exceeded", "maximum context length", "context window exceeded",
        "exceeds the context window", "prompt is too long", "input tokens exceed",
    ))
