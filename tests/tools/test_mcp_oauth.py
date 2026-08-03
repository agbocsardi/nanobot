"""Tests for MCP OAuth wiring — file token storage + provider factory UX handlers."""
from __future__ import annotations

import json
import os
import time

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

import nanobot.agent.tools.mcp_oauth as mcp_oauth
from nanobot.agent.tools.mcp_oauth import (
    FileTokenStorage,
    build_oauth_provider,
    has_stored_token,
)


def _isolate_storage(monkeypatch, tmp_path):
    """Redirect token storage to a tmp dir so tests don't touch ~/.nanobot."""
    monkeypatch.setattr(mcp_oauth, "get_runtime_subdir", lambda _name: tmp_path)


@pytest.mark.asyncio
async def test_file_token_storage_roundtrip(monkeypatch, tmp_path):
    _isolate_storage(monkeypatch, tmp_path)

    storage = FileTokenStorage("coros")
    assert await storage.get_tokens() is None
    assert await storage.get_client_info() is None

    token = OAuthToken(access_token="abc", refresh_token="def", expires_in=3600)
    await storage.set_tokens(token)
    client_info = OAuthClientInformationFull(
        client_id="cid-123",
        client_secret="sec",
        redirect_uris=["http://localhost:8765/callback"],
        token_endpoint_auth_method="client_secret_post",
    )
    await storage.set_client_info(client_info)

    # A fresh instance reads the same file (simulates a restart).
    reloaded = FileTokenStorage("coros")
    got = await reloaded.get_tokens()
    assert got is not None and got.access_token == "abc" and got.refresh_token == "def"
    info = await reloaded.get_client_info()
    assert info is not None and info.client_id == "cid-123"


@pytest.mark.asyncio
async def test_provider_restores_expired_persisted_token_deadline(monkeypatch, tmp_path):
    _isolate_storage(monkeypatch, tmp_path)
    storage = FileTokenStorage("coros")
    await storage.set_tokens(
        OAuthToken(access_token="expired", refresh_token="refresh", expires_in=3600)
    )
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id="cid-123",
            redirect_uris=["http://localhost:8765/callback"],
            token_endpoint_auth_method="none",
        )
    )
    data = json.loads(storage.path.read_text())
    data["token_expires_at"] = time.time() - 60
    storage.path.write_text(json.dumps(data))

    provider = build_oauth_provider(
        "https://mcp.coros.com/mcp", "coros", interactive=False
    )
    await provider._initialize()

    assert provider.context.token_expiry_time is not None
    assert provider.context.token_expiry_time <= time.time()
    assert provider.context.is_token_valid() is False
    assert provider.context.can_refresh_token() is True


@pytest.mark.asyncio
async def test_provider_migrates_legacy_token_expiry_from_file_mtime(monkeypatch, tmp_path):
    _isolate_storage(monkeypatch, tmp_path)
    storage = FileTokenStorage("coros")
    await storage.set_tokens(
        OAuthToken(access_token="expired", refresh_token="refresh", expires_in=60)
    )
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id="cid-123",
            redirect_uris=["http://localhost:8765/callback"],
            token_endpoint_auth_method="none",
        )
    )
    data = json.loads(storage.path.read_text())
    data.pop("token_expires_at")
    storage.path.write_text(json.dumps(data))
    expired_mtime = time.time() - 120
    os.utime(storage.path, (expired_mtime, expired_mtime))

    provider = build_oauth_provider(
        "https://mcp.coros.com/mcp", "coros", interactive=False
    )
    await provider._initialize()

    assert provider.context.token_expiry_time is not None
    assert provider.context.is_token_valid() is False
    assert provider.context.can_refresh_token() is True
    assert "token_expires_at" in json.loads(storage.path.read_text())


@pytest.mark.asyncio
async def test_has_stored_token_gates_on_file(monkeypatch, tmp_path):
    _isolate_storage(monkeypatch, tmp_path)
    assert await has_stored_token("coros") is False
    await FileTokenStorage("coros").set_tokens(OAuthToken(access_token="abc"))
    assert await has_stored_token("coros") is True


@pytest.mark.asyncio
async def test_non_interactive_handlers_raise(monkeypatch, tmp_path):
    """Gateway mode must never block: a needed full re-auth skips the server."""
    _isolate_storage(monkeypatch, tmp_path)
    provider = build_oauth_provider("https://mcp.coros.com/mcp", "coros", interactive=False)
    with pytest.raises(RuntimeError, match="interactive auth required"):
        await provider.context.redirect_handler("https://example/authorize")
    with pytest.raises(RuntimeError, match="interactive auth required"):
        await provider.context.callback_handler()


@pytest.mark.asyncio
async def test_interactive_callback_handler_parses_code(monkeypatch, tmp_path):
    """First-auth parses `code`+`state` out of the pasted redirect URL."""
    _isolate_storage(monkeypatch, tmp_path)
    provider = build_oauth_provider("https://mcp.coros.com/mcp", "coros", interactive=True)
    pasted = "http://localhost:8765/callback?code=AC-123&state=st-xyz"
    monkeypatch.setattr("builtins.input", lambda _prompt: pasted)
    code, state = await provider.context.callback_handler()
    assert code == "AC-123"
    assert state == "st-xyz"


@pytest.mark.asyncio
async def test_interactive_callback_handler_rejects_missing_code(monkeypatch, tmp_path):
    _isolate_storage(monkeypatch, tmp_path)
    provider = build_oauth_provider("https://mcp.coros.com/mcp", "coros", interactive=True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "http://localhost:8765/callback?error=denied")
    with pytest.raises(ValueError, match="no `code`"):
        await provider.context.callback_handler()
