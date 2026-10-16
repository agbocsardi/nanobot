"""Lossless idle tool-result projection; never mutate the stored transcript."""

from typing import Any

MICROCOMPACT_THROUGH = "idle_microcompact_through"
_KEEP_RECENT = 10
_MIN_CHARS = 500
_COMPACTABLE_TOOLS = frozenset({
    "read_file", "exec", "grep", "find_files", "web_search", "web_fetch",
    "list_dir", "list_exec_sessions",
})


def microcompact(
    messages: list[dict[str, Any]], through_entry_id: str | None,
) -> list[dict[str, Any]]:
    """Hide older text results only through the last persisted idle boundary.

    Fresh tool results are never hidden before the model sees them. Images
    retain the existing vision/image-budget policy. Missing/stale boundaries
    fail open to the original results, not to loss of context.
    """
    if not through_entry_id:
        return messages
    end = next((i for i, message in enumerate(messages)
                if message.get("_entry_id") == through_entry_id), None)
    if end is None:
        return messages
    eligible = [i for i, message in enumerate(messages[:end + 1])
                if message.get("role") == "tool"
                and message.get("name") in _COMPACTABLE_TOOLS]
    updated = list(messages)
    for index in eligible[:-_KEEP_RECENT]:
        message = messages[index]
        content = message.get("content")
        is_blocks = isinstance(content, list)
        if is_blocks:
            if any(
                not isinstance(block, dict)
                or block.get("type") != "text"
                or not isinstance(block.get("text"), str)
                or block["text"].startswith("[image: ")
                for block in content
            ):
                continue
            text = "".join(block["text"] for block in content)
        else:
            text = content if isinstance(content, str) else ""
        if len(text) >= _MIN_CHARS:
            placeholder = f"[{message['name']} result omitted from context]"
            updated[index] = {
                **message,
                "content": [{"type": "text", "text": placeholder}] if is_blocks else placeholder,
            }
    return updated
