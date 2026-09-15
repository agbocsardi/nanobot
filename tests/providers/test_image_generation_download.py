"""SSRF validation on provider-returned image download URLs.

Provider payloads are untrusted: the original URL and every redirect hop must
pass ``validate_url_target`` before a request is made.
"""

from __future__ import annotations

import base64
import socket
from dataclasses import dataclass, field
from unittest.mock import patch

import httpx
import pytest

from nanobot.providers.image_generation import (
    ImageGenerationError,
    _download_image_data_url,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
_DATA_URL = "data:image/png;base64," + base64.b64encode(_PNG).decode("ascii")


@dataclass
class FakeResponse:
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    url: str = ""
    text: str = ""
    closed: bool = False

    async def aclose(self) -> None:
        self.closed = True

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=httpx.Request("GET", self.url or "http://test/"),
                response=httpx.Response(self.status_code),
            )


class FakeClient:
    """Plays back queued responses and records every requested URL."""

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.requested: list[str] = []

    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        assert kwargs.get("follow_redirects") is False, "redirects must be manual"
        self.requested.append(url)
        return self.responses.pop(0)


def _fake_dns(hosts: dict[str, list[str]]):
    def _resolver(hostname, port, family=0, type_=0):
        ips = hosts.get(hostname)
        if ips is None:
            raise socket.gaierror(f"cannot resolve {hostname}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in ips]

    return _resolver


def test_private_url_blocked_before_any_request():
    client = FakeClient()
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_dns({"10.0.0.5": ["10.0.0.5"]})):
        with pytest.raises(ImageGenerationError, match="blocked"):
            import asyncio

            asyncio.run(_download_image_data_url(client, "http://10.0.0.5/img.png"))
    assert client.requested == []


def test_metadata_ip_blocked_before_any_request():
    client = FakeClient()
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_dns({"169.254.169.254": ["169.254.169.254"]})):
        with pytest.raises(ImageGenerationError, match="blocked"):
            import asyncio

            asyncio.run(_download_image_data_url(client, "http://169.254.169.254/latest/meta-data/"))
    assert client.requested == []


def test_non_http_scheme_rejected():
    client = FakeClient()
    with pytest.raises(ImageGenerationError, match="blocked"):
        import asyncio

        asyncio.run(_download_image_data_url(client, "file:///etc/passwd"))
    assert client.requested == []


def test_public_to_private_redirect_blocked():
    first = FakeResponse(
        status_code=302,
        headers={"location": "http://metadata.internal/secret"},
        url="http://cdn.example.com/img.png",
    )
    client = FakeClient(first)
    dns = _fake_dns({
        "cdn.example.com": ["93.184.216.34"],
        "metadata.internal": ["169.254.169.254"],
    })
    with patch("nanobot.security.network.socket.getaddrinfo", dns):
        with pytest.raises(ImageGenerationError, match="redirect blocked"):
            import asyncio

            asyncio.run(_download_image_data_url(client, "http://cdn.example.com/img.png"))
    # Only the first (public) hop was ever requested.
    assert client.requested == ["http://cdn.example.com/img.png"]
    assert first.closed


def test_public_to_loopback_redirect_blocked():
    client = FakeClient(
        FakeResponse(
            status_code=301,
            headers={"location": "http://127.0.0.1:9222/json"},
            url="http://cdn.example.com/a.png",
        ),
    )
    dns = _fake_dns({
        "cdn.example.com": ["93.184.216.34"],
        "127.0.0.1": ["127.0.0.1"],
    })
    with patch("nanobot.security.network.socket.getaddrinfo", dns):
        with pytest.raises(ImageGenerationError, match="redirect blocked"):
            import asyncio

            asyncio.run(_download_image_data_url(client, "http://cdn.example.com/a.png"))
    assert client.requested == ["http://cdn.example.com/a.png"]


def test_public_download_and_public_redirect_succeed():
    client = FakeClient(
        FakeResponse(
            status_code=302,
            headers={"location": "/real/img.png"},
            url="http://cdn.example.com/img.png",
        ),
        FakeResponse(status_code=200, content=_PNG, url="http://cdn.example.com/real/img.png"),
    )
    dns = _fake_dns({"cdn.example.com": ["93.184.216.34"]})
    with patch("nanobot.security.network.socket.getaddrinfo", dns):
        import asyncio

        result = asyncio.run(_download_image_data_url(client, "http://cdn.example.com/img.png"))
    assert result == _DATA_URL
    assert client.requested == [
        "http://cdn.example.com/img.png",
        "http://cdn.example.com/real/img.png",
    ]


def test_error_status_raises_image_generation_error():
    client = FakeClient(
        FakeResponse(status_code=404, url="http://cdn.example.com/missing.png", text="nope"),
    )
    dns = _fake_dns({"cdn.example.com": ["93.184.216.34"]})
    with patch("nanobot.security.network.socket.getaddrinfo", dns):
        with pytest.raises(ImageGenerationError, match="failed to download"):
            import asyncio

            asyncio.run(_download_image_data_url(client, "http://cdn.example.com/missing.png"))
