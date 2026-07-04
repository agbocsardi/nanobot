"""Tests for the MCP HTTP probe guard — screens unreachable / auth-failing servers before entering the anyio transports (prevents the event-loop crash from GH #10)."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from nanobot.agent.tools.mcp import _probe_http_url, connect_mcp_servers
from nanobot.agent.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# _probe_http_url unit tests
# ---------------------------------------------------------------------------

async def _http_server(status: int, reason: str = "OK"):
    """Start a tiny HTTP/1.1 server on a random port that replies with ``status``
    to any request. Returns ``(server, port)``."""
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await reader.read(4096)
            if not chunk:
                break
            buf += chunk
        head, _, body = buf.partition(b"\r\n\r\n")
        content_length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                content_length = int(line.split(b":", 1)[1].strip())
        if len(body) < content_length:
            try:
                await reader.readexactly(content_length - len(body))
            except asyncio.IncompleteReadError:
                pass
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.mark.asyncio
async def test_probe_returns_true_when_server_responds_ok():
    """A live server returning 2xx means reachable + authed → proceed."""
    server, port = await _http_server(200)
    try:
        assert await _probe_http_url(f"http://127.0.0.1:{port}/mcp") is True
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_probe_returns_false_on_401():
    """401 = auth failed → skip. This is the GH #10 crash root cause."""
    server, port = await _http_server(401, "Unauthorized")
    try:
        assert await _probe_http_url(f"http://127.0.0.1:{port}/mcp") is False
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_probe_returns_false_on_403():
    """403 = auth failed → skip."""
    server, port = await _http_server(403, "Forbidden")
    try:
        assert await _probe_http_url(f"http://127.0.0.1:{port}/mcp") is False
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_probe_returns_false_for_closed_port():
    """Port 19999 is almost certainly not listening."""
    assert await _probe_http_url("http://127.0.0.1:19999/mcp") is False


@pytest.mark.asyncio
async def test_probe_uses_default_port_for_http():
    """When no port in URL, should default to 80 (will fail -> False)."""
    assert await _probe_http_url("http://unreachable-host.test/mcp") is False


# ---------------------------------------------------------------------------
# connect_mcp_servers skips unreachable HTTP servers
# ---------------------------------------------------------------------------

def _make_http_cfg(url: str, transport: str = "streamableHttp"):
    cfg = MagicMock()
    cfg.type = transport
    cfg.url = url
    cfg.command = None
    cfg.args = []
    cfg.env = {}
    cfg.headers = None
    cfg.tool_timeout = 30
    cfg.enabled_tools = ["*"]
    cfg.oauth = False
    return cfg


@pytest.mark.asyncio
async def test_connect_skips_unreachable_streamable_http():
    """Unreachable streamableHttp server should be skipped with a warning, no crash."""
    async def _unreachable(_url: str, **_kw) -> bool:
        return False

    registry = ToolRegistry()
    servers = {"dead": _make_http_cfg("http://93.184.216.34:19999/mcp")}
    with patch("nanobot.agent.tools.mcp._probe_http_url", _unreachable):
        stacks = await connect_mcp_servers(servers, registry)
    assert stacks == {}
    assert len(registry._tools) == 0


@pytest.mark.asyncio
async def test_connect_skips_unreachable_sse():
    """Unreachable SSE server should be skipped with a warning, no crash."""
    async def _unreachable(_url: str, **_kw) -> bool:
        return False

    registry = ToolRegistry()
    servers = {"dead": _make_http_cfg("http://93.184.216.34:19999/sse", transport="sse")}
    with patch("nanobot.agent.tools.mcp._probe_http_url", _unreachable):
        stacks = await connect_mcp_servers(servers, registry)
    assert stacks == {}
    assert len(registry._tools) == 0


@pytest.mark.asyncio
async def test_probe_not_called_for_stdio():
    """stdio transport should not be probed — it spawns a local process."""
    called = False
    original_probe = _probe_http_url

    async def _spy_probe(url, **kw):
        nonlocal called
        called = True
        return await original_probe(url, **kw)

    with patch("nanobot.agent.tools.mcp._probe_http_url", _spy_probe):
        cfg = MagicMock()
        cfg.type = "stdio"
        cfg.url = None
        cfg.command = "nonexistent-command-xyz"
        cfg.args = []
        cfg.env = None
        cfg.headers = None
        cfg.tool_timeout = 30
        cfg.enabled_tools = ["*"]
        registry = ToolRegistry()
        await connect_mcp_servers({"s": cfg}, registry)

    assert not called, "probe should not be called for stdio transport"
