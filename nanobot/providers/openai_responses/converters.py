"""Convert Chat Completions messages/tools to Responses API format."""

from __future__ import annotations

import json
from typing import Any

from nanobot.providers.base import tool_arguments_json_for_replay


def convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Convert Chat Completions messages to Responses API input items.

    Returns ``(system_prompt, input_items)`` where *system_prompt* is extracted
    from any ``system`` role message and *input_items* is the Responses API
    ``input`` array.
    """
    system_prompt = ""
    input_items: list[dict[str, Any]] = []
    used_item_ids: set[str] = set()

    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            system_prompt = content if isinstance(content, str) else ""
            continue

        if role == "user":
            input_items.append(convert_user_message(content))
            continue

        if role == "assistant":
            if isinstance(content, str) and content:
                message_id = _unique_item_id(f"msg_{idx}", used_item_ids)
                input_items.append({
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": content}],
                    "status": "completed", "id": message_id,
                })
            for tool_call in msg.get("tool_calls", []) or []:
                fn = tool_call.get("function") or {}
                call_id, item_id = split_tool_call_id(tool_call.get("id"))
                response_item_id = _unique_item_id(item_id or f"fc_{idx}", used_item_ids)
                input_items.append({
                    "type": "function_call",
                    "id": response_item_id,
                    "call_id": call_id or f"call_{idx}",
                    "name": fn.get("name"),
                    "arguments": tool_arguments_json_for_replay(fn.get("arguments")),
                })
            continue

        if role == "tool":
            call_id, _ = split_tool_call_id(msg.get("tool_call_id"))
            output_text, images = _convert_tool_output(content)
            # Responses function_call_output only accepts text. Do not stringify
            # image data into it: that loses vision semantics and can consume
            # the whole context window. Keep the tool result for call legality,
            # then expose images in a separate user multimodal item.
            input_items.append({"type": "function_call_output", "call_id": call_id, "output": output_text})
            if images:
                input_items.append({
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Tool image output:"},
                        *({"type": "input_image", "image_url": url, "detail": "auto"} for url in images),
                    ],
                })

    return system_prompt, input_items


def _convert_tool_output(content: Any) -> tuple[str, list[str]]:
    """Return text function output and image URLs from a tool result.

    Images cannot be embedded in a Responses ``function_call_output`` (its
    ``output`` field is text). Extract them into a following user item instead
    of serialising potentially very large data URLs as JSON text.
    """
    if not isinstance(content, list):
        return (content if isinstance(content, str) else json.dumps(content, ensure_ascii=False), [])

    text_parts: list[str] = []
    images: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image_url":
            image = block.get("image_url") or {}
            url = image.get("url") if isinstance(image, dict) else None
            if isinstance(url, str) and url:
                images.append(url)
                meta = block.get("_meta") or {}
                path = meta.get("path") if isinstance(meta, dict) else None
                text_parts.append(f"[image: {path}]" if path else "[image]")
        elif block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
        else:
            text_parts.append(json.dumps(block, ensure_ascii=False))
    if not images:
        return json.dumps(content, ensure_ascii=False), []
    return "\n".join(text_parts), images


def convert_user_message(content: Any) -> dict[str, Any]:
    """Convert a user message's content to Responses API format.

    Handles plain strings, ``text`` blocks -> ``input_text``, and
    ``image_url`` blocks -> ``input_image``.
    """
    if isinstance(content, str):
        return {"role": "user", "content": [{"type": "input_text", "text": content}]}
    if isinstance(content, list):
        converted: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                converted.append({"type": "input_text", "text": item.get("text", "")})
            elif item.get("type") == "image_url":
                url = (item.get("image_url") or {}).get("url")
                if url:
                    converted.append({"type": "input_image", "image_url": url, "detail": "auto"})
        if converted:
            return {"role": "user", "content": converted}
    return {"role": "user", "content": [{"type": "input_text", "text": ""}]}


def convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI function-calling tool schema to Responses API flat format."""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        fn = (tool.get("function") or {}) if tool.get("type") == "function" else tool
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {}
        converted.append({
            "type": "function",
            "name": name,
            "description": fn.get("description") or "",
            "parameters": params if isinstance(params, dict) else {},
        })
    return converted


def _unique_item_id(item_id: str, used: set[str]) -> str:
    """Return a Responses input item id that is unique within one request."""
    if item_id not in used:
        used.add(item_id)
        return item_id

    suffix = 2
    while f"{item_id}_{suffix}" in used:
        suffix += 1
    unique = f"{item_id}_{suffix}"
    used.add(unique)
    return unique


def split_tool_call_id(tool_call_id: Any) -> tuple[str, str | None]:
    """Split a compound ``call_id|item_id`` string.

    Returns ``(call_id, item_id)`` where *item_id* may be ``None``.
    """
    if isinstance(tool_call_id, str) and tool_call_id:
        if "|" in tool_call_id:
            call_id, item_id = tool_call_id.split("|", 1)
            return call_id, item_id or None
        return tool_call_id, None
    return "call_0", None
