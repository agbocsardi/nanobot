"""Helpers for runtime model preset selection."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nanobot.config.schema import ModelPresetConfig
from nanobot.providers.base import LLMProvider
from nanobot.providers.factory import ProviderSnapshot, build_provider_snapshot, make_provider

PresetSnapshotLoader = Callable[[str], ProviderSnapshot]


def default_selection_signature(signature: tuple[object, ...] | None) -> tuple[object, ...] | None:
    return signature[:2] if signature else None


def configured_model_presets(config: Any) -> dict[str, ModelPresetConfig]:
    return {**config.model_presets, "default": config.resolve_default_preset()}


def make_preset_snapshot_loader(
    config: Any,
    provider_snapshot_loader: Callable[..., ProviderSnapshot] | None,
) -> PresetSnapshotLoader:
    if provider_snapshot_loader is not None:
        return lambda name: provider_snapshot_loader(preset_name=name)
    return lambda name: build_provider_snapshot(config, preset_name=name)


def build_static_preset_snapshot(
    provider: LLMProvider,
    name: str,
    preset: ModelPresetConfig,
) -> ProviderSnapshot:
    provider.generation = preset.to_generation_settings()
    return ProviderSnapshot(
        provider=provider,
        model=preset.model,
        context_window_tokens=preset.context_window_tokens,
        signature=("model_preset", name, preset.model_dump_json()),
    )


def build_runtime_preset_snapshot(
    *,
    name: str,
    presets: dict[str, ModelPresetConfig],
    provider: LLMProvider,
    loader: PresetSnapshotLoader | None,
) -> ProviderSnapshot:
    if loader is not None:
        return loader(name)
    return build_static_preset_snapshot(provider, name, presets[name])


def normalize_preset_name(name: str | None, presets: dict[str, ModelPresetConfig]) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("model_preset must be a non-empty string")
    name = name.strip()
    if name not in presets:
        raise KeyError(f"model_preset {name!r} not found. Available: {', '.join(presets) or '(none)'}")
    return name


def resolve_run_preset_name(
    config: Any,
    kind: str,
    override: str | None = None,
    *,
    fallback_kind: str | None = None,
) -> str:
    """Resolve model preset name for a run kind.

    Order: explicit override → agents.defaults.run_presets[kind] → active
    agents.defaults.model_preset → implicit default.
    """
    if override and override.strip():
        return override.strip()
    presets = getattr(config.agents.defaults, "run_presets", {}) or {}
    if kind in presets and str(presets[kind]).strip():
        return str(presets[kind]).strip()
    if fallback_kind and fallback_kind in presets and str(presets[fallback_kind]).strip():
        return str(presets[fallback_kind]).strip()
    active = getattr(config.agents.defaults, "model_preset", None)
    if active and active.strip():
        return active.strip()
    return "default"


def build_run_provider_snapshot(
    config: Any,
    kind: str,
    *,
    override: str | None = None,
    allow_raw_model: bool = False,
    fallback_kind: str | None = None,
) -> ProviderSnapshot:
    """Build the provider/model snapshot for a named run kind.

    `allow_raw_model` exists only for legacy Dream `model_override`, which used
    to accept either a preset name or a raw model id.
    """
    name = resolve_run_preset_name(config, kind, override=override, fallback_kind=fallback_kind)
    if name == "default" or name in config.model_presets:
        return build_provider_snapshot(config, preset_name=name)
    if not allow_raw_model:
        raise KeyError(f"model_preset {name!r} not found for run kind {kind!r}")
    preset = config.resolve_default_preset()
    provider = make_provider(config, preset=preset, model=name)
    return ProviderSnapshot(
        provider=provider,
        model=name,
        context_window_tokens=preset.context_window_tokens,
        signature=("run_preset_raw_model", kind, name),
    )

