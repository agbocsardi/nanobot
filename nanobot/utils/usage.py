"""Tiny token-usage helpers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

USAGE_TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")
USAGE_KEYS = (*USAGE_TOKEN_KEYS, "requests")


def _clean_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_usage(raw: dict[str, Any] | None) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    usage = {key: _clean_int(raw.get(key)) for key in USAGE_TOKEN_KEYS}
    if usage["total_tokens"] <= 0:
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return usage if usage["total_tokens"] > 0 else {}


def add_usage(
    total: dict[str, Any],
    raw: dict[str, Any] | None,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> bool:
    usage = normalize_usage(raw)
    if not usage:
        return False
    for key in USAGE_TOKEN_KEYS:
        total[key] = _clean_int(total.get(key)) + usage[key]
    total["requests"] = _clean_int(total.get("requests")) + 1

    if provider or model:
        provider = provider or "unknown"
        model = model or "unknown"
        by_model = total.setdefault("by_model", {})
        row = by_model.setdefault(f"{provider}/{model}", {"provider": provider, "model": model})
        row["provider"] = provider
        row["model"] = model
        for key in USAGE_TOKEN_KEYS:
            row[key] = _clean_int(row.get(key)) + usage[key]
        row["requests"] = _clean_int(row.get("requests")) + 1
    return True


def usage_delta(total: dict[str, Any] | None, previous: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(total, dict):
        return {}
    previous = previous if isinstance(previous, dict) else {}
    out: dict[str, Any] = {}
    for key in USAGE_KEYS:
        value = _clean_int(total.get(key)) - _clean_int(previous.get(key))
        if value > 0:
            out[key] = value

    by_model: dict[str, Any] = {}
    total_models = total.get("by_model") if isinstance(total.get("by_model"), dict) else {}
    previous_models = previous.get("by_model") if isinstance(previous.get("by_model"), dict) else {}
    for model_key, row in total_models.items():
        if not isinstance(row, dict):
            continue
        prev = previous_models.get(model_key) if isinstance(previous_models, dict) else {}
        prev = prev if isinstance(prev, dict) else {}
        delta_row: dict[str, Any] = {
            "provider": str(row.get("provider") or "unknown"),
            "model": str(row.get("model") or "unknown"),
        }
        for key in USAGE_KEYS:
            value = _clean_int(row.get(key)) - _clean_int(prev.get(key))
            if value > 0:
                delta_row[key] = value
        if any(key in delta_row for key in USAGE_KEYS):
            by_model[str(model_key)] = delta_row
    if by_model:
        out["by_model"] = by_model
    return out if any(key in out for key in USAGE_KEYS) else {}


def usage_snapshot(raw: dict[str, Any] | None) -> dict[str, Any]:
    return deepcopy(raw) if isinstance(raw, dict) else {}
