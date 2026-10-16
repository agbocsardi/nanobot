"""Tests for the vision handoff (bound raw images, describe the older ones)."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nanobot.agent.vision_handoff import VisionHandoff
from nanobot.providers.base import LLMProvider, LLMResponse

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_IMG_URL = f"data:image/png;base64,{base64.b64encode(_PNG).decode()}"


def _image_block(url: str = _IMG_URL, path: str = "x.png") -> dict:
    return {"type": "image_url", "image_url": {"url": url}, "_meta": {"path": path}}


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


def _make(handoff_models=("umans-glm-5.2",), *, max_raw_images: int = 9) -> VisionHandoff:
    return VisionHandoff(
        _fake_describer(),
        "umans-flash",
        handoff_models=frozenset(handoff_models),
        max_raw_images=max_raw_images,
    )


def _image_block(url: str = _IMG_URL, path: str = "x.png") -> dict:
    return {"type": "image_url", "image_url": {"url": url}, "_meta": {"path": path}}


def _distinct_describer(calls: dict | None = None) -> MagicMock:
    """Describer that echoes a per-image label so results are distinguishable."""
    prov = MagicMock(spec=LLMProvider)

    async def chat_with_retry(*, messages, model, max_tokens, temperature, **_):
        url = messages[-1]["content"][0]["image_url"]["url"]
        if calls is not None:
            calls["n"] = calls.get("n", 0) + 1
        return LLMResponse(content=f"desc:{url}", usage={"total_tokens": 10})

    prov.chat_with_retry = chat_with_retry
    prov.generation = SimpleNamespace(max_tokens=1024, temperature=0.2, reasoning_effort=None)
    return prov


def _n_images(n: int) -> list[dict]:
    return [
        {"role": "user", "content": [_image_block(f"{_IMG_URL}#{i}", f"img{i}.png")]}
        for i in range(n)
    ]


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


def test_raw_image_cap_is_zero_for_text_only_and_n_for_vision():
    vh = _make(max_raw_images=9)
    assert vh.raw_image_cap("umans-glm-5.2") == 0   # flagged text-only
    assert vh.raw_image_cap("kimi-k2.6") == 9       # vision-capable
    assert vh.raw_image_cap("umans-flash") == 9     # the describer is vision-capable


@pytest.mark.asyncio
async def test_keeps_newest_raw_and_describes_the_older_ones():
    prov = _distinct_describer()
    vh = VisionHandoff(prov, "umans-flash", max_raw_images=3)
    messages = _n_images(5)

    out = await vh.transform(messages, "kimi-k2.6")

    # Oldest 2 digested, newest 3 left raw.
    for i in (0, 1):
        text = out[i]["content"][0]["text"]
        assert text.startswith("[The user attached an image.")
        assert f"desc:{_IMG_URL}#{i}" in text
    for i in (2, 3, 4):
        assert out[i]["content"][0]["type"] == "image_url"
    # The persisted conversation is untouched.
    assert messages[0]["content"][0]["type"] == "image_url"


@pytest.mark.asyncio
async def test_described_image_carries_its_path_for_re_reading():
    vh = VisionHandoff(_distinct_describer(), "umans-flash", max_raw_images=0)
    out = await vh.transform(
        [{"role": "user", "content": [_image_block(path="/tmp/pic.png")]}],
        "kimi-k2.6",
    )
    text = out[0]["content"][0]["text"]
    assert "[image: /tmp/pic.png; re-read via read_file(/tmp/pic.png)]" in text


@pytest.mark.asyncio
async def test_under_the_cap_makes_no_describer_calls():
    calls: dict = {}
    vh = VisionHandoff(_distinct_describer(calls), "umans-flash", max_raw_images=9)
    messages = _n_images(9)

    out = await vh.transform(messages, "kimi-k2.6")

    assert out is messages
    assert calls.get("n", 0) == 0


@pytest.mark.asyncio
async def test_identical_images_are_described_once():
    calls: dict = {}
    vh = VisionHandoff(_distinct_describer(calls), "umans-flash", max_raw_images=0)
    messages = [
        {"role": "user", "content": [_image_block(), _image_block()]},
    ]

    await vh.transform(messages, "kimi-k2.6")

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_distinct_images_are_described_concurrently():
    """A long session must not serialize describer calls (turn-timeout risk)."""
    import asyncio

    prov = MagicMock(spec=LLMProvider)
    in_flight = {"now": 0, "peak": 0}

    async def chat_with_retry(*, messages, model, max_tokens, temperature, **_):
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        await asyncio.sleep(0.01)
        in_flight["now"] -= 1
        return LLMResponse(content="ok", usage={"total_tokens": 10})

    prov.chat_with_retry = chat_with_retry
    prov.generation = SimpleNamespace(max_tokens=1024, temperature=0.2, reasoning_effort=None)
    vh = VisionHandoff(prov, "umans-flash", max_raw_images=0)

    await vh.transform(_n_images(4), "kimi-k2.6")

    assert in_flight["peak"] > 1


@pytest.mark.asyncio
async def test_cache_eviction_does_not_grow_locks_unbounded():
    vh = VisionHandoff(_distinct_describer(), "umans-flash", max_raw_images=0)
    vh._cache_max = 2

    await vh.transform(_n_images(4), "kimi-k2.6")

    assert len(vh._cache) == 2
    assert len(vh._locks) <= 2

