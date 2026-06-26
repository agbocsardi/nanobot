"""Tests for the vision handoff (describe images for text-only models)."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nanobot.agent.vision_handoff import VisionHandoff
from nanobot.providers.base import LLMProvider, LLMResponse

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_IMG_URL = f"data:image/png;base64,{base64.b64encode(_PNG).decode()}"


def _image_block() -> dict:
    return {"type": "image_url", "image_url": {"url": _IMG_URL}, "_meta": {"path": "x.png"}}


def _fake_describer(content: str = "a red square") -> MagicMock:
    prov = MagicMock(spec=LLMProvider)

    async def chat_with_retry(*, messages, model, max_tokens, temperature, **_):
        # Sanity: the describer receives the image block + a system prompt.
        assert model == "umans-flash"
        user = messages[-1]["content"]
        assert any(isinstance(b, dict) and b.get("type") == "image_url" for b in user)
        return LLMResponse(content=content, usage={"total_tokens": 10})

    prov.chat_with_retry = chat_with_retry
    prov.generation = SimpleNamespace(max_tokens=1024, temperature=0.2, reasoning_effort=None)
    return prov


def _make(handoff_models=("umans-glm-5.2",)) -> VisionHandoff:
    return VisionHandoff(
        _fake_describer(),
        "umans-flash",
        handoff_models=frozenset(handoff_models),
    )


def test_should_handoff_only_for_flagged_text_only_models():
    vh = _make()
    assert vh.should_handoff("umans-glm-5.2")           # flagged text-only
    assert not vh.should_handoff("umans-flash")          # the describer itself
    assert not vh.should_handoff("kimi-k2.6")            # not flagged (vision)
    assert not vh.should_handoff(None)


@pytest.mark.asyncio
async def test_transform_swaps_image_for_description_and_preserves_text():
    vh = _make()
    messages = [
        {"role": "user", "content": [_image_block(), {"type": "text", "text": "what is this?"}]},
    ]
    out = await vh.transform(messages, "umans-glm-5.2")

    assert out is not messages  # new list, persisted untouched
    assert messages[0]["content"][0]["type"] == "image_url"  # original unmutated
    blocks = out[0]["content"]
    assert blocks[0]["text"].endswith("a red square")
    assert blocks[0]["text"].startswith("[The user attached an image.")
    assert blocks[1] == {"type": "text", "text": "what is this?"}


@pytest.mark.asyncio
async def test_transform_caches_description_per_image():
    vh = _make()
    calls = {"n": 0}
    orig = vh._call_describer

    async def counting(block):
        calls["n"] += 1
        return await orig(block)

    vh._call_describer = counting  # type: ignore[method-assign]

    messages = [{"role": "user", "content": [_image_block()]}]
    await vh.transform(messages, "umans-glm-5.2")
    await vh.transform(messages, "umans-glm-5.2")  # same image → cache hit
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_transform_noop_for_vision_model():
    prov = _fake_describer()
    calls = {"n": 0}
    orig = prov.chat_with_retry

    async def counting(**kw):
        calls["n"] += 1
        return await orig(**kw)

    prov.chat_with_retry = counting  # type: ignore[method-assign]
    vh = VisionHandoff(prov, "umans-flash", handoff_models=frozenset({"umans-glm-5.2"}))
    messages = [{"role": "user", "content": [_image_block()]}]
    out = await vh.transform(messages, "kimi-k2.6")  # not flagged → vision-capable
    assert out is messages
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_transform_noop_when_no_images():
    vh = _make()
    messages = [{"role": "user", "content": "just text"}]
    out = await vh.transform(messages, "umans-glm-5.2")
    assert out is messages


@pytest.mark.asyncio
async def test_describer_failure_yields_placeholder_and_is_not_cached():
    prov = MagicMock(spec=LLMProvider)

    async def boom(*a, **k):
        raise RuntimeError("describer down")

    prov.chat_with_retry = boom
    prov.generation = SimpleNamespace(max_tokens=1024, temperature=0.2, reasoning_effort=None)
    vh = VisionHandoff(prov, "umans-flash", handoff_models=frozenset({"umans-glm-5.2"}))

    out = await vh.transform(
        [{"role": "user", "content": [_image_block()]}],
        "umans-glm-5.2",
    )
    assert out[0]["content"][0]["text"].endswith("[image: description unavailable]")
    # Failure must not poison the cache — next turn re-attempts.
    assert len(vh._cache) == 0
