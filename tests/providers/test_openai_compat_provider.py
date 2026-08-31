"""Tests for OpenAICompatProvider content-shape handling.

DeepSeek text chat/reasoner endpoints require ``message.content`` to be a
plain string, so non-string content is coerced.  DeepSeek's vision model
(deepseek-v4-flash-vision-exp) accepts OpenAI-compatible content blocks
(text + image_url parts), so the list structure must be preserved for it.
Other providers are untouched.
"""

from __future__ import annotations

from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import find_by_name


def _deepseek_provider(default_model: str = "deepseek-chat") -> OpenAICompatProvider:
    return OpenAICompatProvider(
        api_key="test-key",
        default_model=default_model,
        spec=find_by_name("deepseek"),
    )


def _vision_content() -> list[dict[str, str]]:
    return [
        {"type": "text", "text": "describe this image"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ]


def _build_kwargs(
    provider: OpenAICompatProvider,
    content,
    model: str | None,
) -> dict:
    return provider._build_kwargs(
        messages=[{"role": "user", "content": content}],
        tools=None,
        model=model,
        max_tokens=1024,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    )


def test_deepseek_text_model_coerces_list_content_to_string() -> None:
    """DeepSeek text chat endpoint expects message.content to be a string."""
    provider = _deepseek_provider(default_model="deepseek-chat")

    kw = _build_kwargs(
        provider,
        [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ],
        model="deepseek-chat",
    )

    assert isinstance(kw["messages"][0]["content"], str)
    assert "hello" in kw["messages"][0]["content"]
    assert "world" in kw["messages"][0]["content"]


def test_deepseek_vision_preserves_multimodal_content() -> None:
    """DeepSeek's vision model requires OpenAI-compatible content blocks."""
    provider = _deepseek_provider(default_model="deepseek-v4-flash-vision-exp")
    content = _vision_content()

    kw = _build_kwargs(provider, content, model="deepseek-v4-flash-vision-exp")

    assert kw["messages"][0]["content"] == content


def test_deepseek_vision_default_model_preserves_content_blocks() -> None:
    """Sanitizing without an explicit model uses the provider default_model."""
    provider = _deepseek_provider(default_model="deepseek-v4-flash-vision-exp")
    content = _vision_content()

    kw = _build_kwargs(provider, content, model=None)

    assert kw["messages"][0]["content"] == content


def test_non_deepseek_keeps_list_content() -> None:
    """Only DeepSeek forces string content; OpenAI-compatible providers keep blocks."""
    provider = OpenAICompatProvider(
        api_key="test-key",
        default_model="gpt-4o",
        spec=find_by_name("openai"),
    )

    kw = _build_kwargs(
        provider,
        [{"type": "text", "text": "hello"}],
        model="gpt-4o",
    )

    assert isinstance(kw["messages"][0]["content"], list)


def test_deepseek_vision_empty_text_block_dropped_image_kept() -> None:
    """Empty text parts are dropped but image parts survive empty-content pass."""
    provider = _deepseek_provider(default_model="deepseek-v4-flash-vision-exp")
    content = [
        {"type": "text", "text": ""},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ]

    kw = _build_kwargs(provider, content, model="deepseek-v4-flash-vision-exp")

    messages = kw["messages"][0]["content"]
    assert isinstance(messages, list)
    assert messages == [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ]


def test_deepseek_vision_responses_body_converts_image_parts() -> None:
    """Responses API body keeps the two content blocks as input_text/input_image."""
    provider = _deepseek_provider(default_model="deepseek-v4-flash-vision-exp")

    body = provider._build_responses_body(
        messages=[{"role": "user", "content": _vision_content()}],
        tools=None,
        model="deepseek-v4-flash-vision-exp",
        max_tokens=1024,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    )

    assert body["input"] == [{
        "role": "user",
        "content": [
            {"type": "input_text", "text": "describe this image"},
            {"type": "input_image", "image_url": "data:image/png;base64,AA==", "detail": "auto"},
        ],
    }]
