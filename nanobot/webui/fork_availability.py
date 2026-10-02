"""Fork-honest availability stubs for upstream-only capability management.

This fork intentionally dropped the API-server process chain, the channel
plugin architecture, and the optional-dependency (pip-from-WebUI) manager.
The ported WebUI settings surface still references those capabilities, so
these stubs keep it importable while making every managed operation report
honestly unavailable (501-class protocol errors) — never fake success.

Error protocol mirrors the pinned upstream (d0d0a44e): ``.message``/``.status``
consumed by the settings route error handlers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_UNAVAILABLE = "unavailable_in_this_build"
_API_UNAVAILABLE = "api_server_unavailable_in_this_build"
_CHANNEL_SETUP_UNAVAILABLE = "channel_setup_unavailable_in_this_build"
_FEATURES_UNAVAILABLE = "optional_feature_management_unavailable_in_this_build"

# Route field-type contract from the pinned upstream channel contracts
# (d0d0a44e); only the alias is needed by the ported settings surface.
RouteFieldType = str | tuple[str, set[str]]


class OptionalFeatureError(Exception):
    """Protocol error for optional-feature management operations."""

    def __init__(self, message: str, *, status: int = 501) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class ChannelConnectError(Exception):
    """User-facing channel connection failure."""

    def __init__(self, message: str, *, status: int = 501) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ApiStartOptions:
    """Accepted for protocol compatibility; never used to start anything."""

    data: dict[str, Any] = field(default_factory=dict)

    def __init__(self, **kwargs: Any) -> None:
        object.__setattr__(self, "data", dict(kwargs))


@dataclass(frozen=True)
class _UnavailableApiStatus:
    running: bool = False
    managed: bool = False
    log_path: str = ""


class UnavailableApiRuntime:
    """API-server runtime stand-in: reports installed/running False, refuses actions."""

    def status(self) -> _UnavailableApiStatus:
        return _UnavailableApiStatus()

    async def start(self, options: ApiStartOptions | None = None) -> dict[str, Any]:
        raise OptionalFeatureError(_API_UNAVAILABLE)

    async def stop(self) -> dict[str, Any]:
        raise OptionalFeatureError(_API_UNAVAILABLE)


# Protocol-compatible alias: the ported settings surface imports ApiRuntime.
ApiRuntime = UnavailableApiRuntime


def api_runtime_paths(config_path: Any) -> None:
    """No API process exists in this build; paths are meaningless."""
    return None


def optional_features_payload(*, config: Any = None, **_: Any) -> dict[str, Any]:
    return {
        "installed": False,
        "reason": _FEATURES_UNAVAILABLE,
        "features": [],
    }


def extra_installed(_group: str, _spec: Any = None) -> bool:
    return False


def optional_dependency_groups() -> dict[str, Any]:
    return {}


def enable_optional_feature(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise OptionalFeatureError(_FEATURES_UNAVAILABLE)


def disable_optional_feature(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise OptionalFeatureError(_FEATURES_UNAVAILABLE)


def install_optional_feature_support(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise OptionalFeatureError(_FEATURES_UNAVAILABLE)


def with_channel_runtime_status(
    payload: dict[str, Any],
    runtime_status: dict[str, Any],
) -> dict[str, Any]:
    """Overlay live ChannelManager state on configuration-derived features.

    Faithful port of the pinned upstream overlay: matches live channel
    statuses to configured features by owner so enabled/disabled badges
    reflect the running gateway. No plugin architecture involved.
    """
    statuses_by_owner: dict[str, list[dict[str, Any]]] = {}
    for status in runtime_status.values():
        if not isinstance(status, dict):
            continue
        owner = status.get("owner")
        if isinstance(owner, str):
            statuses_by_owner.setdefault(owner, []).append(status)

    features: list[dict[str, Any]] = []
    for raw_feature in payload.get("features", []):
        if not isinstance(raw_feature, dict):
            features.append(raw_feature)
            continue
        feature = dict(raw_feature)
        if feature.get("type") != "channel":
            features.append(feature)
            continue
        name = feature.get("name")
        statuses = statuses_by_owner.get(name) if isinstance(name, str) else None
        if statuses:
            feature["running"] = any(bool(s.get("running")) for s in statuses)
        features.append(feature)
    result = dict(payload)
    result["features"] = features
    return result


def channel_setup_spec(_name: str, *, plugin: Any = None) -> None:
    """No channel setup contracts exist in this fork (config.json is the source)."""
    return None


def channel_instance_config(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise OptionalFeatureError(_CHANNEL_SETUP_UNAVAILABLE)


def channel_update_instance_config(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise OptionalFeatureError(_CHANNEL_SETUP_UNAVAILABLE)


# OAuth helpers referenced by the ported settings surface. The fork dropped
# the oauth provider modules (github_copilot/xai/openai_codex_oauth); the
# ported flows already degrade gracefully on ImportError, so only the
# module-level symbols need honest stand-ins.
OAUTH_CLI_KIT_MISSING_MESSAGE = "OAuth CLI kit is not available in this build."


def get_oauth_model_catalog(_name: str, *, proxy: Any = None) -> Any:
    raise OptionalFeatureError("oauth_model_catalog_unavailable_in_this_build")


def invalidate_oauth_model_catalog(_name: str) -> None:
    return None


def load_channel_plugin(_name: str) -> Any:
    """No channel plugin registry in this fork; setup/connect flows refuse."""
    raise OptionalFeatureError(_CHANNEL_SETUP_UNAVAILABLE)


def validate_channel_config(
    name: str,
    raw_values: dict[str, Any] | None = None,
    *,
    instance_id: str = "default",
) -> dict[str, Any]:
    """Report channel setup validation as unsupported (no setup contracts)."""
    return {
        "name": name,
        "status": "unsupported",
        "checks": [
            {
                "id": "channel_setup",
                "label": "Channel setup",
                "status": "fail",
                "message": _CHANNEL_SETUP_UNAVAILABLE,
            }
        ],
        "identity": {},
        "missing_fields": [],
        "can_enable": None,
    }


class AgentPlugin:
    """Annotation target only; this fork ships no agent plugin registry."""


def discover_agent_plugins(_workspace_path: Any) -> list[Any]:
    """No agent plugin architecture in this fork: none to discover."""
    return []


def set_agent_plugin_enabled(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise OptionalFeatureError("agent_plugin_management_unavailable_in_this_build")
