"""MCP-spec OAuth 2.1 client wiring.

Uses the ``mcp`` library's :class:`OAuthClientProvider` (Protected Resource
Metadata discovery + Dynamic Client Registration + PKCE + automatic refresh) as
an :class:`httpx.Auth` on the MCP HTTP client, so bearer-token MCP servers
(e.g. COROS) no longer need a manually-pasted static token that expires.

First-run auth is interactive via ``nanobot mcp auth <server>``: it prints the
authorize URL; you approve on your phone and paste the redirect URL back. The
access + refresh tokens are stored to ``<data-dir>/mcp_tokens/<server>.json``;
thereafter the gateway refreshes automatically — no browser, no re-auth.

The gateway itself never blocks: with no stored token it skips the server
(clear log, "run ``nanobot mcp auth <server>``"), and if a refresh ever fails
the non-interactive handlers raise so the server is skipped instead of hanging
on stdin.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlparse

from loguru import logger

from nanobot.config.paths import get_runtime_subdir


def _token_path(name: str):
    return get_runtime_subdir("mcp_tokens") / f"{name}.json"


class FileTokenStorage:
    """File-backed ``TokenStorage`` for :class:`OAuthClientProvider`.

    Stores access/refresh tokens and DCR client info as JSON so they survive
    restarts and can be seeded on one machine then copied to a headless server.
    """

    def __init__(self, name: str):
        self.path = _token_path(name)

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text()) if self.path.exists() else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2, default=str))

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        data = self._load().get("tokens")
        return OAuthToken.model_validate(data) if data else None

    async def set_tokens(self, tokens) -> None:
        data = self._load()
        data["tokens"] = tokens.model_dump(mode="json")
        self._save(data)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        data = self._load().get("client_info")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info) -> None:
        data = self._load()
        data["client_info"] = client_info.model_dump(mode="json")
        self._save(data)


async def has_stored_token(name: str) -> bool:
    """True if a usable token file exists for this server (gateway skip-gate)."""
    return bool(await FileTokenStorage(name).get_tokens())


def build_oauth_provider(
    server_url: str,
    name: str,
    *,
    interactive: bool,
    redirect_port: int = 8765,
    scopes: str = "openid mcp.tools offline_access",
):
    """Build an :class:`OAuthClientProvider` wired to file storage + UX handlers.

    ``interactive=True`` (the ``nanobot mcp auth`` command): prints the authorize
    URL and reads the pasted redirect URL from stdin. ``interactive=False``
    (gateway): handlers raise, so a needed full re-auth skips the server instead
    of blocking on stdin.
    """
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    storage = FileTokenStorage(name)
    client_metadata = OAuthClientMetadata(
        redirect_uris=[f"http://localhost:{redirect_port}/callback"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scopes,
        client_name="nanobot",
    )

    if interactive:
        async def redirect_handler(auth_url: str) -> None:
            logger.warning("MCP OAuth [{}]: open this URL and approve:\n  {}", name, auth_url)

        async def callback_handler() -> tuple[str, str | None]:
            url = await asyncio.to_thread(input, "Paste the full redirect URL you ended up on: ")
            params = parse_qs(urlparse(url.strip()).query)
            code = (params.get("code") or [None])[0]
            state = (params.get("state") or [None])[0]
            if not code:
                raise ValueError("no `code` parameter in the pasted URL")
            return code, state

    else:
        hint = f"run `nanobot mcp auth {name}`"

        async def redirect_handler(auth_url: str) -> None:
            raise RuntimeError(f"MCP OAuth [{name}]: interactive auth required ({hint})")

        async def callback_handler() -> tuple[str, str | None]:
            raise RuntimeError(f"MCP OAuth [{name}]: interactive auth required ({hint})")

    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=300.0,
    )
