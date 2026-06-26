"""Vision handoff: describe images for text-only models via a vision model.

When the active model cannot see images, ``image_url`` blocks in the
outgoing messages are replaced with text descriptions produced by a
vision-capable describer model (e.g. umans-flash) before the request
leaves. Minimal form of the pi-vision-handoff pattern: one describer call
per image, cached by image hash for the process lifetime.

The persisted conversation is never mutated — only the LLM-bound copy is
transformed, so images stay in storage for inline rendering / resume.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from typing import Any

from loguru import logger

from nanobot.providers.base import LLMProvider

_DESCRIBE_SYSTEM = (
    "You are a precise vision assistant. Describe the image exhaustively so a "
    "text-only model can act on it. Cover: scene/subject, any visible text (quote "
    "verbatim), UI elements and layout, colors, and anything task-relevant "
    "(errors, diagrams, code, data). Be concrete and structured; do not speculate "
    "beyond what is visible."
)


class VisionHandoff:
    """Describe images via a vision model for text-only target models.

    Built once per ``AgentLoop``. Descriptions are cached per image hash; a
    describer failure is not cached so the next turn re-attempts.
    """

    def __init__(
        self,
        describer: LLMProvider,
        describer_model: str,
        *,
        handoff_models: frozenset[str] = frozenset(),
        max_description_tokens: int = 1024,
        prompt: str | None = None,
    ):
        self._describer = describer
        self._model = describer_model
        self._handoff_models = frozenset(m.lower() for m in handoff_models)
        self._max_tokens = max_description_tokens
        self._system = prompt or _DESCRIBE_SYSTEM
        # LRU by image hash; bounded so a long session cannot grow unbounded.
        self._cache: "OrderedDict[str, str]" = OrderedDict()
        self._cache_max = 64
        self._lock = asyncio.Lock()

    def should_handoff(self, model: str | None) -> bool:
        """True when *model* is a flagged text-only model (and not the describer)."""
        m = (model or "").lower()
        if not m or m == self._model.lower():
            return False
        return m in self._handoff_models

    async def transform(
        self,
        messages: list[dict[str, Any]],
        model: str | None,
    ) -> list[dict[str, Any]]:
        """Return a copy of *messages* with image_url blocks swapped for text.

        Returns the input unchanged when the model is vision-capable or no
        images are present. Persisted message dicts are never mutated.
        """
        if not self.should_handoff(model):
            return messages

        image_idxs = [
            i for i, msg in enumerate(messages)
            if isinstance(msg.get("content"), list)
            and any(isinstance(b, dict) and b.get("type") == "image_url" for b in msg["content"])
        ]
        if not image_idxs:
            return messages

        new_messages = list(messages)
        for i in image_idxs:
            new_content: list[Any] = []
            for block in messages[i]["content"]:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    desc = await self._describe(block)
                    new_content.append({"type": "text", "text": f"[image]\n{desc}"})
                else:
                    new_content.append(block)
            new_messages[i] = {**messages[i], "content": new_content}
        return new_messages

    async def _describe(self, image_block: dict[str, Any]) -> str:
        url = (image_block.get("image_url") or {}).get("url") or ""
        key = hashlib.sha256(url.encode()).hexdigest()
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        # ponytail: one lock around the call — serializes describer calls but
        # avoids duplicate concurrent calls for the same image. Per-image locks
        # if throughput matters.
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
            desc = await self._call_describer(image_block)
            if desc:
                self._cache[key] = desc
                if len(self._cache) > self._cache_max:
                    self._cache.popitem(last=False)
            return desc or "[image: description unavailable]"

    async def _call_describer(self, image_block: dict[str, Any]) -> str:
        try:
            resp = await self._describer.chat_with_retry(
                messages=[
                    {"role": "system", "content": self._system},
                    {
                        "role": "user",
                        "content": [
                            image_block,
                            {"type": "text", "text": "Describe this image."},
                        ],
                    },
                ],
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=0.2,
            )
        except Exception:
            logger.exception("vision handoff describer call failed")
            return ""
        return (resp.content or "").strip()


def build_from_config(config: Any) -> VisionHandoff | None:
    """Construct a VisionHandoff from config, or None if disabled/misconfigured."""
    from nanobot.providers.factory import make_provider

    cfg = config.agents.defaults.vision_handoff
    if not cfg.enabled:
        return None
    preset_name = cfg.describer_preset
    preset = config.model_presets.get(preset_name)
    if preset is None:
        logger.warning(
            "vision handoff disabled: describer_preset {!r} not in model_presets",
            preset_name,
        )
        return None
    try:
        describer = make_provider(config, preset_name=preset_name)
    except Exception:
        logger.exception("vision handoff disabled: could not build describer provider")
        return None
    handoff_models = frozenset(
        p.model for p in config.model_presets.values() if p.needs_vision_handoff
    )
    return VisionHandoff(
        describer,
        preset.model,
        handoff_models=handoff_models,
        max_description_tokens=cfg.max_description_tokens,
        prompt=cfg.prompt,
    )
