"""Read-only Discord history tool with a late-bound runtime handle."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, tool_parameters

DISCORD_AVAILABLE = importlib.util.find_spec("discord") is not None
if DISCORD_AVAILABLE:
    import discord
if TYPE_CHECKING:
    import discord


_DEFAULT_LIMIT = 500
_MAX_LIMIT = 2000
_DEFAULT_PER_SOURCE = 200
_MAX_PER_SOURCE = 500
_DEFAULT_ARCHIVED = 50
_MAX_ARCHIVED = 200
_DEFAULT_MAX_CHARS = 12000
_MAX_MAX_CHARS = 16000


class DiscordRuntimeHandle:
    """Late-bound mutable holder for the live Discord channel.

    Created before ``AgentLoop`` construction (so tool registration can
    retain it) and bound to the running ``DiscordChannel`` after
    ``ChannelManager`` starts. ``DiscordHistoryTool`` resolves the
    connected client through this handle at execution time, which keeps
    tool registration independent of the Discord client lifecycle and
    makes the same bound handle available to isolated cron turns that
    share the agent's tool registry.
    """

    def __init__(self) -> None:
        self._channel: Any = None

    def bind(self, channel: Any) -> None:
        """Bind the handle to a running ``DiscordChannel`` (or None to clear)."""
        self._channel = channel

    @property
    def channel(self) -> Any | None:
        """The bound ``DiscordChannel`` (may be None if Discord is disabled)."""
        return self._channel

    def resolve_client(self) -> Any | None:
        """Return the connected, ready discord.py client, or None."""
        ch = self._channel
        if ch is None:
            return None
        client = ch.client
        if client is None or not client.is_ready():
            return None
        return client


def _parse_iso(value: str | None, field: str) -> datetime | None | str:
    """Parse an ISO-8601 datetime; return None, or an error string."""
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return f"Error: {field} must be ISO-8601; got {value!r}"
    if dt.tzinfo is None:
        return f"Error: {field} must be timezone-aware; got {value!r}"
    return dt.astimezone(timezone.utc)


def _valid_snowflake(value: str | None, field: str) -> str | None:
    """Return None if valid, or an error string."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.isdigit():
        return f"Error: {field} must be a numeric Discord id string"
    return None


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "guild_id": {
                "type": "string",
                "description": (
                    "Discord guild id. May be omitted only when the bot is in "
                    "exactly one guild; otherwise required."
                ),
            },
            "channel_id": {
                "type": "string",
                "description": (
                    "Fetch only this text channel, forum post/thread, or thread. "
                    "When omitted, scan eligible configured sources in the guild."
                ),
            },
            "since": {
                "type": "string",
                "description": "ISO-8601 lower bound (exclusive, UTC normalized).",
            },
            "until": {
                "type": "string",
                "description": "ISO-8601 upper bound (exclusive, UTC normalized).",
            },
            "limit": {
                "type": "integer",
                "description": f"Global max returned messages (default {_DEFAULT_LIMIT}, max {_MAX_LIMIT}).",
                "minimum": 1,
                "maximum": _MAX_LIMIT,
            },
            "per_source_limit": {
                "type": "integer",
                "description": f"Max messages fetched from one source (default {_DEFAULT_PER_SOURCE}, max {_MAX_PER_SOURCE}).",
                "minimum": 1,
                "maximum": _MAX_PER_SOURCE,
            },
            "include_threads": {
                "type": "boolean",
                "description": "Include accessible active threads/forum posts (default true).",
            },
            "include_archived_threads": {
                "type": "boolean",
                "description": (
                    "Also include archived public threads/posts and joined private "
                    "archives (default false)."
                ),
            },
            "archived_thread_limit": {
                "type": "integer",
                "description": f"Max archived threads enumerated per parent (default {_DEFAULT_ARCHIVED}, max {_MAX_ARCHIVED}).",
                "minimum": 1,
                "maximum": _MAX_ARCHIVED,
            },
            "max_chars": {
                "type": "integer",
                "description": f"Serialized result budget (default {_DEFAULT_MAX_CHARS}, max {_MAX_MAX_CHARS}).",
                "minimum": 1,
                "maximum": _MAX_MAX_CHARS,
            },
        },
    }
)
class DiscordHistoryTool(Tool):
    """Fetch past Discord messages from one configured guild (read-only)."""

    _scopes = {"core"}

    def __init__(self, handle: DiscordRuntimeHandle) -> None:
        self._handle = handle

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.discord_runtime_handle is not None

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(handle=ctx.discord_runtime_handle)

    @property
    def name(self) -> str:
        return "discord_history"

    @property
    def description(self) -> str:
        return (
            "Fetch past Discord messages from one configured guild as a bounded, "
            "read-only structured result. Respects the bot's allow_channels and "
            "effective view_channel + read_message_history permissions. Requires "
            "the MESSAGE_CONTENT intent for text content."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        *,
        guild_id: str | None = None,
        channel_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = _DEFAULT_LIMIT,
        per_source_limit: int = _DEFAULT_PER_SOURCE,
        include_threads: bool = True,
        include_archived_threads: bool = False,
        archived_thread_limit: int = _DEFAULT_ARCHIVED,
        max_chars: int = _DEFAULT_MAX_CHARS,
        **kwargs: Any,
    ) -> str:
        if not DISCORD_AVAILABLE:
            return (
                "Error: discord.py is not installed. "
                "Run: pip install nanobot-ai[discord]"
            )

        handle = self._handle
        channel = handle.channel
        client = handle.resolve_client()
        if channel is None or client is None:
            return (
                "Error: Discord is not connected or not ready. "
                "This tool only works in the gateway with Discord enabled."
            )

        # Some providers serialize omitted optional string parameters as "".
        # Treat blank Discord ids as omitted instead of rejecting an otherwise
        # valid guild-wide discovery request.
        if isinstance(guild_id, str) and not guild_id.strip():
            guild_id = None
        if isinstance(channel_id, str) and not channel_id.strip():
            channel_id = None

        # --- parameter validation ---
        err = _valid_snowflake(guild_id, "guild_id")
        if err:
            return err
        err = _valid_snowflake(channel_id, "channel_id")
        if err:
            return err
        since_dt = _parse_iso(since, "since")
        if isinstance(since_dt, str):
            return since_dt
        until_dt = _parse_iso(until, "until")
        if isinstance(until_dt, str):
            return until_dt
        if since_dt and until_dt and since_dt >= until_dt:
            return "Error: since must be earlier than until"
        if limit <= 0 or limit > _MAX_LIMIT:
            return f"Error: limit must be in 1..{_MAX_LIMIT}"
        if per_source_limit <= 0 or per_source_limit > _MAX_PER_SOURCE:
            return f"Error: per_source_limit must be in 1..{_MAX_PER_SOURCE}"
        if archived_thread_limit <= 0 or archived_thread_limit > _MAX_ARCHIVED:
            return f"Error: archived_thread_limit must be in 1..{_MAX_ARCHIVED}"
        if max_chars <= 0 or max_chars > _MAX_MAX_CHARS:
            return f"Error: max_chars must be in 1..{_MAX_MAX_CHARS}"

        # --- guild resolution ---
        guild = self._resolve_guild(client, guild_id)
        if isinstance(guild, str):
            return guild

        # --- source collection ---
        if channel_id is not None:
            sources = await self._resolve_explicit_sources(
                channel, client, guild, channel_id,
                include_threads, include_archived_threads, archived_thread_limit,
            )
            if isinstance(sources, str):
                return sources
            skipped = 0
            discovery_warnings: list[str] = []
        else:
            sources, skipped, discovery_warnings = await self._discover_sources(
                channel, client, guild,
                include_threads, include_archived_threads, archived_thread_limit,
            )

        if not sources:
            result = self._empty_result(guild, since_dt, until_dt, skipped, discovery_warnings)
            return self._serialize_result(result, max_chars)

        # --- bounded fetch ---
        collected: list[Any] = []
        warnings = list(discovery_warnings)
        scanned = 0
        for src in sources:
            remaining = limit - len(collected)
            if remaining <= 0:
                break
            fetch_limit = min(per_source_limit, remaining)
            scanned += 1
            try:
                async for msg in src.history(
                    after=since_dt, before=until_dt, limit=fetch_limit, oldest_first=True,
                ):
                    collected.append(msg)
            except Exception as e:
                warnings.append(f"fetch failed in {self._src_label(src)}: {e}")
                continue

        collected.sort(key=lambda m: (m.created_at, m.id))
        if len(collected) > limit:
            collected = collected[:limit]

        messages = [self._serialize_message(m) for m in collected]
        result = {
            "guild": {"id": str(guild.id), "name": guild.name},
            "window": {
                "since": since_dt.isoformat() if since_dt else None,
                "until": until_dt.isoformat() if until_dt else None,
            },
            "messages": messages,
            "sources": {"scanned": scanned, "skipped": skipped},
            "truncated": False,
            "warnings": warnings,
        }
        return self._serialize_result(result, max_chars)

    # --- guild / source resolution ---

    def _resolve_guild(self, client: Any, guild_id: str | None) -> Any | str:
        if guild_id is not None:
            guild = client.get_guild(int(guild_id))
            if guild is None:
                return f"Error: guild {guild_id} not found or not accessible"
            return guild
        guilds = list(client.guilds)
        if len(guilds) == 1:
            return guilds[0]
        if not guilds:
            return "Error: bot is not in any guild"
        return (
            "Error: bot is in multiple guilds; guild_id is required. "
            f"Connected guilds: {len(guilds)}"
        )

    async def _bot_member(self, guild: Any, client: Any) -> Any | None:
        me = getattr(guild, "me", None)
        if me is not None:
            return me
        user = getattr(client, "user", None)
        if user is None:
            return None
        try:
            return await guild.fetch_member(user.id)
        except Exception:
            return None

    def _can_read(self, source: Any, me: Any) -> bool:
        if me is None:
            return False
        try:
            perms = source.permissions_for(me)
        except Exception:
            return False
        return bool(getattr(perms, "view_channel", False)) and bool(
            getattr(perms, "read_message_history", False)
        )

    async def _resolve_explicit_sources(
        self,
        channel: Any,
        client: Any,
        guild: Any,
        channel_id: str,
        include_threads: bool,
        include_archived: bool,
        archived_limit: int,
    ) -> list[Any] | str:
        try:
            cid = int(channel_id)
        except (TypeError, ValueError):
            return "Error: channel_id must be a numeric Discord id string"
        src = client.get_channel(cid)
        if src is None:
            try:
                src = await client.fetch_channel(cid)
            except Exception:
                src = None
        if src is None:
            return "Error: channel not available to this bot configuration"
        src_guild = getattr(src, "guild", None)
        if src_guild is None or src_guild.id != guild.id:
            return "Error: channel not available to this bot configuration"
        if not channel.is_channel_allowed(src):
            return "Error: channel not available to this bot configuration"
        me = await self._bot_member(guild, client)

        if isinstance(src, discord.ForumChannel):
            posts: list[Any] = []
            if include_threads:
                for thread in src.threads:
                    if channel.is_channel_allowed(thread) and self._can_read(thread, me):
                        posts.append(thread)
            if include_archived:
                try:
                    async for thread in src.archived_threads(limit=archived_limit):
                        if channel.is_channel_allowed(thread) and self._can_read(thread, me):
                            posts.append(thread)
                except Exception as e:
                    return f"Error: failed enumerating archived forum posts: {e}"
            if not posts:
                return "Error: no accessible threads in that forum"
            return posts

        if isinstance(src, discord.Thread):
            if not self._can_read(src, me):
                return "Error: channel not available to this bot configuration"
            return [src]

        if isinstance(src, discord.TextChannel):
            if not self._can_read(src, me):
                return "Error: channel not available to this bot configuration"
            return [src]

        return "Error: channel not available to this bot configuration"

    async def _discover_sources(
        self,
        channel: Any,
        client: Any,
        guild: Any,
        include_threads: bool,
        include_archived: bool,
        archived_limit: int,
    ) -> tuple[list[Any], int, list[str]]:
        sources: list[Any] = []
        skipped = 0
        warnings: list[str] = []
        seen: set[int] = set()
        me = await self._bot_member(guild, client)

        def try_add(src: Any) -> None:
            nonlocal skipped
            if src is None:
                return
            sid = getattr(src, "id", None)
            if sid is None or sid in seen:
                return
            if not channel.is_channel_allowed(src):
                skipped += 1
                return
            if not self._can_read(src, me):
                skipped += 1
                return
            seen.add(sid)
            sources.append(src)

        for tc in guild.text_channels:
            try_add(tc)

        # discord.py exposes forums through Guild.channels; Guild.forum_channels
        # does not exist (including in 2.7.x).
        forums = [
            source for source in guild.channels if isinstance(source, discord.ForumChannel)
        ]

        if include_threads:
            for forum in forums:
                if not channel.is_channel_allowed(forum) or not self._can_read(forum, me):
                    continue
                for thread in forum.threads:
                    try_add(thread)
            try:
                active = await guild.active_threads()
            except Exception as e:
                warnings.append(f"active_threads failed: {e}")
                active = []
            for thread in active:
                try_add(thread)

        if include_archived:
            for tc in list(sources):
                if not isinstance(tc, discord.TextChannel):
                    continue
                try:
                    async for thread in tc.archived_threads(
                        private=False, joined=False, limit=archived_limit
                    ):
                        try_add(thread)
                    async for thread in tc.archived_threads(
                        private=True, joined=True, limit=archived_limit
                    ):
                        try_add(thread)
                except Exception as e:
                    warnings.append(f"archived_threads failed in {tc.name}: {e}")
            for forum in forums:
                if not channel.is_channel_allowed(forum) or not self._can_read(forum, me):
                    continue
                try:
                    async for thread in forum.archived_threads(limit=archived_limit):
                        try_add(thread)
                except Exception as e:
                    warnings.append(f"archived_threads failed in {forum.name}: {e}")

        return sources, skipped, warnings

    # --- serialization ---

    def _empty_result(
        self,
        guild: Any,
        since_dt: datetime | None,
        until_dt: datetime | None,
        skipped: int,
        warnings: list[str],
    ) -> dict[str, Any]:
        return {
            "guild": {"id": str(guild.id), "name": guild.name},
            "window": {
                "since": since_dt.isoformat() if since_dt else None,
                "until": until_dt.isoformat() if until_dt else None,
            },
            "messages": [],
            "sources": {"scanned": 0, "skipped": skipped},
            "truncated": False,
            "warnings": warnings,
        }

    def _serialize_message(self, msg: Any) -> dict[str, Any]:
        ch = msg.channel
        author = msg.author
        author_name = (
            getattr(author, "display_name", None)
            or getattr(author, "name", None)
            or str(getattr(author, "id", ""))
        )
        if isinstance(ch, discord.Thread):
            parent = getattr(ch, "parent", None)
            channel_id = str(parent.id) if parent else None
            channel_name = getattr(parent, "name", None) if parent else None
            thread_id = str(ch.id)
            thread_name = getattr(ch, "name", None)
        else:
            channel_id = str(getattr(ch, "id", ""))
            channel_name = getattr(ch, "name", None)
            thread_id = None
            thread_name = None
        ref = getattr(msg, "reference", None)
        reply_to = (
            str(ref.message_id)
            if ref and getattr(ref, "message_id", None)
            else None
        )
        mtype = getattr(msg, "type", None)
        mtype_name = getattr(mtype, "name", None) or str(mtype)
        attachments = [
            {"filename": a.filename, "url": a.url} for a in (msg.attachments or [])
        ]
        embeds = [
            {
                "type": getattr(e, "type", None),
                "title": getattr(e, "title", None),
                "url": getattr(e, "url", None),
            }
            for e in (msg.embeds or [])
        ]
        created_at = getattr(msg, "created_at", None)
        return {
            "message_id": str(msg.id),
            "channel_id": channel_id,
            "channel": channel_name,
            "thread_id": thread_id,
            "thread": thread_name,
            "author_id": str(getattr(author, "id", "")),
            "author": author_name,
            "timestamp": created_at.isoformat() if created_at else None,
            "message_type": mtype_name,
            "content": msg.content or "",
            "reply_to": reply_to,
            "attachments": attachments,
            "embeds": embeds,
        }

    def _src_label(self, src: Any) -> str:
        name = getattr(src, "name", None) or ""
        return f"{getattr(src, 'id', '?')}:{name}"

    def _serialize_result(self, result: dict[str, Any], max_chars: int) -> str:
        all_msgs = result["messages"]
        prefix = {k: v for k, v in result.items() if k != "messages"}
        prefix_str = json.dumps(prefix, separators=(",", ":"), ensure_ascii=False)
        # prefix_str includes `"messages":[]`; the real payload replaces `[]`
        # with the kept messages, so reserve two chars for that swap.
        budget = max_chars - len(prefix_str) + 2
        parts: list[str] = []
        total = 0
        truncated = False
        for m in all_msgs:
            mj = json.dumps(m, separators=(",", ":"), ensure_ascii=False)
            extra = (1 if parts else 0) + len(mj)
            if total + extra > budget:
                truncated = True
                break
            parts.append(mj)
            total += extra
        result["messages"] = [json.loads(p) for p in parts]
        result["truncated"] = truncated or bool(result.get("truncated", False))
        return json.dumps(result, separators=(",", ":"), ensure_ascii=False)
