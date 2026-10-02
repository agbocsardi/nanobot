"""Gateway boot and WebUI HTTP route checks (synthetic config, no LLM).

Boots the fork's ordinary foreground gateway listener with a disposable
synthetic config on an ephemeral port and exercises the HTTP surface the
WebUI depends on: unauthenticated bootstrap is 401, authenticated bootstrap
succeeds, the removed remote-instance routes answer 401 before auth and 501
after, and static serving is clean (404 without a dist, assets with one).
"""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import suppress
from pathlib import Path

import httpx
import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config.loader import load_config, set_config_path
from nanobot.session.manager import SessionManager


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_config(tmp_path: Path, port: int, token: str) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "agents": {"defaults": {"workspace": str(tmp_path / "workspace")}},
        "channels": {
            "websocket": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": port,
                "token": token,
                "path": "/",
            },
        },
    }))
    return config_path


async def _wait_ready(client: httpx.AsyncClient, url: str) -> None:
    for _ in range(100):
        try:
            await client.get(url)
            return
        except httpx.HTTPError:
            await asyncio.sleep(0.05)
    raise AssertionError("listener did not become ready")


@pytest.mark.asyncio
async def test_gateway_boot_routes_and_static(tmp_path) -> None:
    token = "synthetic-token"
    port = _free_port()
    config_path = _write_config(tmp_path, port, token)
    set_config_path(config_path)
    config = load_config(config_path)

    bus = MessageBus()
    sessions = SessionManager(workspace=config.workspace_path)
    manager = ChannelManager(config, bus, session_manager=sessions, cron_service=None)
    channel = manager.channels.get("websocket")
    assert channel is not None
    # Manager wiring composed the WebUI gateway (not legacy gateway-less mode).
    assert channel.gateway is not None
    assert channel.gateway.session_exists("webui:missing") is False

    # start() runs the listener until stop; run it as the manager does.
    start_task = asyncio.create_task(channel.start())
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await _wait_ready(client, f"{base}/webui/bootstrap")

            # Protected routes reject unauthenticated requests first.
            unauth = await client.get(f"{base}/webui/bootstrap")
            assert unauth.status_code == 401
            unauth_remote = await client.get(f"{base}/api/remote-instances")
            assert unauth_remote.status_code == 401
            # An API token is not a free pass before auth either.
            bogus = await client.get(
                f"{base}/api/remote-instances",
                headers={"Authorization": "Bearer not-a-real-token"},
            )
            assert bogus.status_code == 401

            authed = await client.get(f"{base}/webui/bootstrap", headers=headers)
            assert authed.status_code == 200
            bootstrap = authed.json()
            assert isinstance(bootstrap, dict)
            # Static bootstrap token mints a short-lived API token for routes.
            api_token = bootstrap.get("api_token")
            assert isinstance(api_token, str) and api_token
            api_headers = {"Authorization": f"Bearer {api_token}"}

            # Removed remote chain: honest 501 only after authentication.
            remote = await client.get(f"{base}/api/remote-instances", headers=api_headers)
            assert remote.status_code == 501
            assert remote.json()["error"] == "remote_instances_unavailable_in_this_build"

            # No built dist in this checkout: clean 404, never an SPA fallback
            # for API routes.
            missing_api = await client.get(f"{base}/api/does-not-exist", headers=api_headers)
            assert missing_api.status_code == 404
    finally:
        await manager.stop_all()
        with suppress(Exception):
            await asyncio.wait_for(start_task, timeout=5.0)


@pytest.mark.asyncio
async def test_static_dist_serving_and_spa_fallback(tmp_path) -> None:
    """A built dist is served with correct types; unknown API paths stay 404."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>nanobot webui</title>")
    (dist / "assets" / "app.js").write_text("console.log('app')")

    token = "synthetic-token"
    port = _free_port()
    config_path = _write_config(tmp_path, port, token)
    set_config_path(config_path)
    config = load_config(config_path)

    from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
    from nanobot.webui.gateway_services import build_gateway_services

    bus = MessageBus()
    sessions = SessionManager(workspace=config.workspace_path)
    section = {
        "enabled": True, "host": "127.0.0.1", "port": port,
        "token": token, "path": "/",
    }
    parsed = WebSocketConfig.model_validate(section)
    gateway = build_gateway_services(
        config=parsed,
        bus=bus,
        session_manager=sessions,
        static_dist_path=dist,
        workspace_path=config.workspace_path,
        default_restrict_to_workspace=True,
        config_path=config_path,
        runtime_model_name=None,
        refresh_runtime_config=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
        cron_service=None,
        logger=__import__("loguru").logger,
    )
    channel = WebSocketChannel(section, bus, gateway=gateway)

    start_task = asyncio.create_task(channel.start())
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await _wait_ready(client, f"{base}/webui/bootstrap")

            index = await client.get(f"{base}/")
            assert index.status_code == 200
            assert "nanobot webui" in index.text

            asset = await client.get(f"{base}/assets/app.js")
            assert asset.status_code == 200
            assert "javascript" in asset.headers.get("Content-Type", "")

            # SPA fallback serves index for client routes, but never for /api/.
            spa = await client.get(f"{base}/chat/some-route")
            assert spa.status_code == 200
            api_miss = await client.get(f"{base}/api/nope")
            assert api_miss.status_code == 404

            # Path traversal never leaks the config file (httpx normalizes a
            # plain /../, so probe with an encoded form the server must reject).
            traversal = await client.get(f"{base}/%2e%2e%2fconfig.json")
            assert "synthetic-token" not in traversal.text
            assert traversal.status_code in (403, 404) or "nanobot webui" in traversal.text
    finally:
        await channel.stop()
        with suppress(Exception):
            await asyncio.wait_for(start_task, timeout=5.0)
