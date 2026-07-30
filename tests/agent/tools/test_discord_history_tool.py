"""Tests for the read-only discord_history tool and its late-bound handle."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("discord")
import discord  # noqa: E402

from nanobot.agent.tools.context import ToolContext  # noqa: E402
from nanobot.agent.tools.discord_history import (  # noqa: E402
    DiscordHistoryTool,
    DiscordRuntimeHandle,
)
from nanobot.agent.tools.loader import ToolLoader  # noqa: E402
from nanobot.bus.queue import MessageBus  # noqa: E402
from nanobot.channels.discord import DiscordChannel, DiscordConfig  # noqa: E402

# ---------------------------------------------------------------------------
# Fake discord.py object builders (spec-bound so isinstance() works)
# ---------------------------------------------------------------------------


async def _aiter(items):
    for item in items:
        yield item


def _history_factory(messages):
    def _history(*, after=None, before=None, limit=None, oldest_first=None):
        out = list(messages)
        if after is not None:
            out = [m for m in out if m.created_at > after]
        if before is not None:
            out = [m for m in out if m.created_at < before]
        if oldest_first:
            out.sort(key=lambda m: (m.created_at, m.id))
        if limit is not None:
            out = out[:limit]
        return _aiter(out)

    return _history


def _perms(*, view=True, history=True):
    p = MagicMock(spec=discord.Permissions)
    p.view_channel = view
    p.read_message_history = history
    return p


def _make_author(aid, *, name="user", display=None):
    return SimpleNamespace(id=aid, name=name, display_name=display or name)


def _make_msg(
    mid,
    content,
    *,
    channel,
    author=None,
    created_at=None,
    mtype="default",
    reply_to=None,
    attachments=(),
    embeds=(),
):
    msg = SimpleNamespace()
    msg.id = mid
    msg.content = content
    msg.channel = channel
    msg.author = author or _make_author(1)
    msg.created_at = created_at or datetime(2024, 1, 1, tzinfo=timezone.utc)
    msg.type = SimpleNamespace(name=mtype)
    msg.reference = SimpleNamespace(message_id=reply_to) if reply_to else None
    msg.attachments = list(attachments)
    msg.embeds = list(embeds)
    return msg


def _make_member():
    return MagicMock(spec=discord.Member)


def _make_text(cid, name, *, guild, perms=None, messages=(), archived=()):
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = cid
    ch.name = name
    ch.guild = guild
    ch.permissions_for = MagicMock(return_value=perms or _perms())
    ch.history = _history_factory(messages)
    ch.archived_threads = MagicMock(side_effect=lambda **kw: _aiter(list(archived)))
    return ch


def _make_thread(tid, name, *, parent, perms=None, messages=()):
    th = MagicMock(spec=discord.Thread)
    th.id = tid
    th.name = name
    th.parent_id = parent.id
    th.parent = parent
    th.guild = parent.guild
    th.permissions_for = MagicMock(return_value=perms or _perms())
    th.history = _history_factory(messages)
    return th


def _make_forum(fid, name, *, guild, perms=None, threads=(), archived=()):
    ch = MagicMock(spec=discord.ForumChannel)
    ch.id = fid
    ch.name = name
    ch.guild = guild
    ch.permissions_for = MagicMock(return_value=perms or _perms())
    ch.threads = list(threads)
    ch.archived_threads = MagicMock(side_effect=lambda **kw: _aiter(list(archived)))
    return ch


def _make_guild(
    gid=1,
    name="Guild",
    *,
    text_channels=(),
    forums=(),
    active_threads=(),
    me=None,
):
    g = MagicMock(spec=discord.Guild)
    g.id = gid
    g.name = name
    g.text_channels = list(text_channels)
    g.forum_channels = list(forums)
    g.me = me or _make_member()
    g.active_threads = AsyncMock(return_value=list(active_threads))
    return g


def _make_client(guilds):
    registry: dict[int, object] = {}
    for g in guilds:
        for tc in g.text_channels:
            registry[tc.id] = tc
        for f in g.forum_channels:
            registry[f.id] = f
            for t in f.threads:
                registry[t.id] = t
        for t in g.active_threads.return_value:
            registry[t.id] = t

    client = MagicMock(spec=discord.Client)
    client.is_ready.return_value = True
    client.user = SimpleNamespace(id=999)
    client.guilds = list(guilds)
    by_gid = {g.id: g for g in guilds}
    client.get_guild = MagicMock(side_effect=lambda gid: by_gid.get(gid))
    client.get_channel = MagicMock(side_effect=lambda cid: registry.get(cid))

    async def _fetch_channel(cid):
        found = registry.get(cid)
        if found is None:
            raise discord.NotFound(MagicMock(), "unknown channel")
        return found

    client.fetch_channel = AsyncMock(side_effect=_fetch_channel)
    return client


def _make_setup(*, allow_channels=(), guilds=()):
    cfg = DiscordConfig(allow_channels=list(allow_channels))
    ch = DiscordChannel(cfg, MessageBus())
    ch._client = _make_client(guilds)
    handle = DiscordRuntimeHandle()
    handle.bind(ch)
    tool = DiscordHistoryTool(handle=handle)
    return tool, ch


# ---------------------------------------------------------------------------
# Handle lifecycle / readiness
# ---------------------------------------------------------------------------


def test_handle_unbound_resolves_none():
    handle = DiscordRuntimeHandle()
    assert handle.channel is None
    assert handle.resolve_client() is None


def test_enabled_requires_handle():
    ctx_with = ToolContext(config=None, workspace=".", discord_runtime_handle=DiscordRuntimeHandle())
    ctx_without = ToolContext(config=None, workspace=".")
    assert DiscordHistoryTool.enabled(ctx_with) is True
    assert DiscordHistoryTool.enabled(ctx_without) is False


def test_tool_not_registered_without_handle():
    ctx = ToolContext(config=None, workspace=".")
    loader = ToolLoader(test_classes=[DiscordHistoryTool])
    registry = MagicMock()
    registry.has.return_value = False
    loader.load(ctx, registry, scope="core")
    registry.register.assert_not_called()


@pytest.mark.asyncio
async def test_missing_dependency_error(monkeypatch):
    tool, _ = _make_setup(guilds=[_make_guild()])
    monkeypatch.setattr(
        "nanobot.agent.tools.discord_history.DISCORD_AVAILABLE", False
    )
    result = await tool.execute()
    assert "discord.py is not installed" in result


@pytest.mark.asyncio
async def test_not_ready_when_client_unset():
    cfg = DiscordConfig()
    ch = DiscordChannel(cfg, MessageBus())
    handle = DiscordRuntimeHandle()
    handle.bind(ch)  # _client is None
    tool = DiscordHistoryTool(handle=handle)
    result = await tool.execute()
    assert "not connected or not ready" in result


@pytest.mark.asyncio
async def test_not_ready_when_client_not_ready():
    tool, ch = _make_setup(guilds=[_make_guild()])
    ch._client.is_ready.return_value = False
    result = await tool.execute()
    assert "not connected or not ready" in result


# ---------------------------------------------------------------------------
# Guild resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guild_omitted_ok_when_single_guild():
    tc = _make_text(10, "general", guild=None)
    guild = _make_guild(text_channels=[tc])
    tc.guild = guild
    tool, _ = _make_setup(guilds=[guild])
    result = await tool.execute()
    data = json.loads(result)
    assert data["guild"]["id"] == "1"


@pytest.mark.asyncio
async def test_guild_omitted_ambiguous_requires_guild_id():
    g1 = _make_guild(gid=1)
    g2 = _make_guild(gid=2)
    tool, _ = _make_setup(guilds=[g1, g2])
    result = await tool.execute()
    assert "multiple guilds" in result
    assert "guild_id is required" in result


@pytest.mark.asyncio
async def test_guild_not_found():
    tool, _ = _make_setup(guilds=[_make_guild(gid=1)])
    result = await tool.execute(guild_id="999")
    assert "not found" in result


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_snowflake_rejected():
    tool, _ = _make_setup(guilds=[_make_guild()])
    assert "numeric" in await tool.execute(guild_id="abc")
    assert "numeric" in await tool.execute(channel_id="xyz")


@pytest.mark.asyncio
async def test_blank_ids_are_treated_as_omitted():
    tc = _make_text(10, "general", guild=None)
    guild = _make_guild(text_channels=[tc])
    tc.guild = guild
    tc.history = _history_factory([_make_msg(1, "hi", channel=tc)])
    tool, _ = _make_setup(guilds=[guild])

    data = json.loads(await tool.execute(guild_id="", channel_id=" "))

    assert data["guild"]["id"] == "1"
    assert data["messages"][0]["channel_id"] == "10"


@pytest.mark.asyncio
async def test_naive_datetime_rejected():
    tool, _ = _make_setup(guilds=[_make_guild()])
    result = await tool.execute(since="2024-01-01T00:00:00")
    assert "timezone-aware" in result


@pytest.mark.asyncio
async def test_since_greater_equal_until_rejected():
    tool, _ = _make_setup(guilds=[_make_guild()])
    ts = "2024-01-01T00:00:00+00:00"
    assert "earlier than" in await tool.execute(since=ts, until=ts)


@pytest.mark.asyncio
async def test_out_of_range_limits_rejected():
    tool, _ = _make_setup(guilds=[_make_guild()])
    assert "limit must be in" in await tool.execute(limit=0)
    assert "limit must be in" in await tool.execute(limit=99999)
    assert "per_source_limit must be in" in await tool.execute(per_source_limit=0)
    assert "max_chars must be in" in await tool.execute(max_chars=0)


# ---------------------------------------------------------------------------
# Allowlist + permissions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allowlist_denies_unlisted_channel():
    listed = _make_text(10, "general", guild=None)
    unlisted = _make_text(20, "other", guild=None)
    guild = _make_guild(text_channels=[listed, unlisted])
    listed.guild = guild
    unlisted.guild = guild
    listed.history = _history_factory([_make_msg(1, "hi", channel=listed)])
    unlisted.history = _history_factory([_make_msg(2, "nope", channel=unlisted)])
    tool, _ = _make_setup(allow_channels=["10"], guilds=[guild])
    data = json.loads(await tool.execute())
    fetched = {m["channel_id"] for m in data["messages"]}
    assert fetched == {"10"}
    # the unlisted channel was skipped (not a fetch failure, but not scanned)
    assert data["sources"]["skipped"] == 1


@pytest.mark.asyncio
async def test_allowlist_parent_thread_semantics():
    parent = _make_text(10, "general", guild=None)
    thread = _make_thread(100, "topic", parent=parent)
    guild = _make_guild(text_channels=[parent], active_threads=[thread])
    parent.guild = guild
    tool, _ = _make_setup(allow_channels=["10"], guilds=[guild])
    data = json.loads(await tool.execute())
    # thread's parent is listed, so the thread is eligible
    assert any(m["thread_id"] == "100" for m in data["messages"]) or data["sources"]["scanned"] >= 1


@pytest.mark.asyncio
async def test_explicit_disallowed_channel_generic_error():
    unlisted = _make_text(20, "other", guild=None)
    guild = _make_guild(text_channels=[unlisted])
    unlisted.guild = guild
    tool, _ = _make_setup(allow_channels=["10"], guilds=[guild])
    result = await tool.execute(channel_id="20")
    assert "not available to this bot configuration" in result


@pytest.mark.asyncio
async def test_permission_denied_source_skipped():
    ok = _make_text(10, "general", guild=None, messages=[_make_msg(1, "hi", channel=None)])
    locked = _make_text(
        20, "locked", guild=None, perms=_perms(view=False, history=False)
    )
    guild = _make_guild(text_channels=[ok, locked])
    ok.guild = guild
    locked.guild = guild
    ok.messages_ref = [_make_msg(1, "hi", channel=ok)]
    ok.history = _history_factory([_make_msg(1, "hi", channel=ok)])
    tool, _ = _make_setup(guilds=[guild])
    data = json.loads(await tool.execute())
    assert data["sources"]["skipped"] == 1
    assert all(m["channel_id"] == "10" for m in data["messages"])


@pytest.mark.asyncio
async def test_explicit_no_read_permission_generic_error():
    locked = _make_text(20, "locked", guild=None, perms=_perms(view=False, history=False))
    guild = _make_guild(text_channels=[locked])
    locked.guild = guild
    tool, _ = _make_setup(guilds=[guild])
    result = await tool.execute(channel_id="20")
    assert "not available to this bot configuration" in result


# ---------------------------------------------------------------------------
# Output ordering and caps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_chronological_order_across_sources():
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    tc1 = _make_text(10, "a", guild=None)
    tc2 = _make_text(20, "b", guild=None)
    guild = _make_guild(text_channels=[tc1, tc2])
    tc1.guild = guild
    tc2.guild = guild
    tc1.history = _history_factory([
        _make_msg(2, "second", channel=tc1, created_at=base.replace(hour=2)),
        _make_msg(1, "first", channel=tc1, created_at=base.replace(hour=1)),
    ])
    tc2.history = _history_factory([
        _make_msg(3, "third", channel=tc2, created_at=base.replace(hour=3)),
    ])
    tool, _ = _make_setup(guilds=[guild])
    data = json.loads(await tool.execute())
    ids = [int(m["message_id"]) for m in data["messages"]]
    assert ids == [1, 2, 3]


@pytest.mark.asyncio
async def test_global_limit_cap():
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    tc1 = _make_text(10, "a", guild=None)
    tc2 = _make_text(20, "b", guild=None)
    guild = _make_guild(text_channels=[tc1, tc2])
    tc1.guild = guild
    tc2.guild = guild
    tc1.history = _history_factory([
        _make_msg(1, "m1", channel=tc1, created_at=base),
        _make_msg(2, "m2", channel=tc1, created_at=base.replace(hour=1)),
    ])
    tc2.history = _history_factory([
        _make_msg(3, "m3", channel=tc2, created_at=base.replace(hour=2)),
        _make_msg(4, "m4", channel=tc2, created_at=base.replace(hour=3)),
    ])
    tool, _ = _make_setup(guilds=[guild])
    data = json.loads(await tool.execute(limit=2))
    assert len(data["messages"]) == 2
    assert [int(m["message_id"]) for m in data["messages"]] == [1, 2]


@pytest.mark.asyncio
async def test_per_source_limit_cap():
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    tc1 = _make_text(10, "a", guild=None)
    tc2 = _make_text(20, "b", guild=None)
    guild = _make_guild(text_channels=[tc1, tc2])
    tc1.guild = guild
    tc2.guild = guild
    tc1.history = _history_factory([
        _make_msg(1, "m1", channel=tc1, created_at=base),
        _make_msg(2, "m2", channel=tc1, created_at=base.replace(hour=1)),
    ])
    tc2.history = _history_factory([
        _make_msg(3, "m3", channel=tc2, created_at=base.replace(hour=2)),
        _make_msg(4, "m4", channel=tc2, created_at=base.replace(hour=3)),
    ])
    tool, _ = _make_setup(guilds=[guild])
    data = json.loads(await tool.execute(limit=10, per_source_limit=1))
    # one message per source
    assert len(data["messages"]) == 2
    assert {int(m["message_id"]) for m in data["messages"]} == {1, 3}


@pytest.mark.asyncio
async def test_max_chars_truncation_flag():
    tc = _make_text(10, "a", guild=None)
    guild = _make_guild(text_channels=[tc])
    tc.guild = guild
    big = "x" * 500
    tc.history = _history_factory([
        _make_msg(i, big, channel=tc, created_at=datetime(2024, 1, 1, tzinfo=timezone.utc).replace(hour=i))
        for i in range(1, 20)
    ])
    tool, _ = _make_setup(guilds=[guild])
    data = json.loads(await tool.execute(limit=20, max_chars=1000))
    assert data["truncated"] is True
    assert len(data["messages"]) < 20


# ---------------------------------------------------------------------------
# Source discovery: text, forum posts, active threads, archived, dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_channel_direct_fetch():
    tc = _make_text(10, "general", guild=None)
    guild = _make_guild(text_channels=[tc])
    tc.guild = guild
    tc.history = _history_factory([_make_msg(5, "hello", channel=tc)])
    tool, _ = _make_setup(guilds=[guild])
    data = json.loads(await tool.execute(channel_id="10"))
    assert len(data["messages"]) == 1
    assert data["messages"][0]["content"] == "hello"
    assert data["messages"][0]["channel"] == "general"


@pytest.mark.asyncio
async def test_forum_parent_returns_posts():
    forum = _make_forum(50, "dev", guild=None)
    post = _make_thread(500, "intro", parent=forum)
    forum.threads = [post]
    guild = _make_guild(forums=[forum])
    forum.guild = guild
    post.history = _history_factory([_make_msg(7, "post msg", channel=post)])
    tool, _ = _make_setup(guilds=[guild])
    data = json.loads(await tool.execute(channel_id="50"))
    assert len(data["messages"]) == 1
    assert data["messages"][0]["thread_id"] == "500"


@pytest.mark.asyncio
async def test_thread_direct_fetch():
    parent = _make_text(10, "general", guild=None)
    thread = _make_thread(100, "topic", parent=parent)
    guild = _make_guild(text_channels=[parent], active_threads=[thread])
    parent.guild = guild
    thread.guild = guild
    thread.history = _history_factory([_make_msg(8, "in thread", channel=thread)])
    tool, _ = _make_setup(guilds=[guild])
    data = json.loads(await tool.execute(channel_id="100"))
    assert len(data["messages"]) == 1
    assert data["messages"][0]["thread_id"] == "100"
    assert data["messages"][0]["channel"] == "general"


@pytest.mark.asyncio
async def test_active_threads_included_via_discovery():
    parent = _make_text(10, "general", guild=None)
    thread = _make_thread(100, "topic", parent=parent)
    guild = _make_guild(text_channels=[parent], active_threads=[thread])
    parent.guild = guild
    thread.history = _history_factory([_make_msg(9, "msg", channel=thread)])
    tool, _ = _make_setup(guilds=[guild])
    data = json.loads(await tool.execute())
    assert any(m["thread_id"] == "100" for m in data["messages"])


@pytest.mark.asyncio
async def test_archived_public_and_joined_private_threads():
    parent = _make_text(10, "general", guild=None)
    archived_pub = _make_thread(200, "arch-pub", parent=parent)
    archived_priv = _make_thread(300, "arch-priv", parent=parent)
    guild = _make_guild(text_channels=[parent])
    parent.guild = guild
    parent.archived_threads = MagicMock(return_value=_aiter([archived_pub, archived_priv]))
    archived_pub.history = _history_factory([_make_msg(11, "pub", channel=archived_pub)])
    archived_priv.history = _history_factory([_make_msg(12, "priv", channel=archived_priv)])
    tool, _ = _make_setup(guilds=[guild])
    data = json.loads(await tool.execute(include_threads=False, include_archived_threads=True))
    ids = {m["thread_id"] for m in data["messages"]}
    assert ids == {"200", "300"}
    # The tool must request joined private archives only (joined=True).
    parent.archived_threads.assert_any_call(private=True, joined=True, limit=50)
    parent.archived_threads.assert_any_call(private=False, joined=False, limit=50)


@pytest.mark.asyncio
async def test_dedup_thread_from_active_and_archived():
    parent = _make_text(10, "general", guild=None)
    thread = _make_thread(100, "topic", parent=parent)
    guild = _make_guild(text_channels=[parent], active_threads=[thread])
    parent.guild = guild
    # same thread also surfaced via archived enumeration
    parent.archived_threads = MagicMock(return_value=_aiter([thread]))
    thread.history = _history_factory([_make_msg(1, "msg", channel=thread)])
    tool, _ = _make_setup(guilds=[guild])
    data = json.loads(await tool.execute(include_archived_threads=True))
    # fetched once despite two discovery paths
    assert len(data["messages"]) == 1


# ---------------------------------------------------------------------------
# Partial failures, result shape, date filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_failure_records_warning_and_returns_rest():
    good = _make_text(10, "ok", guild=None)
    bad = _make_text(20, "bad", guild=None)
    guild = _make_guild(text_channels=[good, bad])
    good.guild = guild
    bad.guild = guild
    good.history = _history_factory([_make_msg(1, "ok", channel=good)])

    def _boom(**kwargs):
        raise RuntimeError("boom")

    bad.history = _boom
    tool, _ = _make_setup(guilds=[guild])
    data = json.loads(await tool.execute())
    assert len(data["messages"]) == 1
    assert data["warnings"]
    assert any("fetch failed" in w for w in data["warnings"])


@pytest.mark.asyncio
async def test_date_filtering_exclusive_bounds():
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    tc = _make_text(10, "a", guild=None)
    guild = _make_guild(text_channels=[tc])
    tc.guild = guild
    tc.history = _history_factory([
        _make_msg(1, "before", channel=tc, created_at=base),
        _make_msg(2, "in", channel=tc, created_at=base.replace(hour=2)),
        _make_msg(3, "after", channel=tc, created_at=base.replace(hour=5)),
    ])
    tool, _ = _make_setup(guilds=[guild])
    data = json.loads(await tool.execute(
        since=base.isoformat(), until=base.replace(hour=5).isoformat(),
    ))
    ids = [int(m["message_id"]) for m in data["messages"]]
    assert ids == [2]


@pytest.mark.asyncio
async def test_result_shape_fields():
    tc = _make_text(10, "general", guild=None)
    guild = _make_guild(gid=42, name="MyGuild", text_channels=[tc])
    tc.guild = guild
    author = _make_author(7, name="alice", display="Alice")
    att = SimpleNamespace(filename="f.png", url="http://x/f.png")
    emb = SimpleNamespace(type="rich", title="T", url="http://x")
    tc.history = _history_factory([
        _make_msg(
            100, "hi", channel=tc, author=author, reply_to=99,
            attachments=[att], embeds=[emb], mtype="reply",
        )
    ])
    tool, _ = _make_setup(guilds=[guild])
    data = json.loads(await tool.execute())
    assert set(data.keys()) == {"guild", "window", "messages", "sources", "truncated", "warnings"}
    assert data["guild"] == {"id": "42", "name": "MyGuild"}
    assert data["truncated"] is False
    m = data["messages"][0]
    assert m["message_id"] == "100"
    assert m["channel_id"] == "10"
    assert m["channel"] == "general"
    assert m["thread_id"] is None
    assert m["author"] == "Alice"
    assert m["author_id"] == "7"
    assert m["reply_to"] == "99"
    assert m["message_type"] == "reply"
    assert m["attachments"] == [{"filename": "f.png", "url": "http://x/f.png"}]
    assert m["embeds"] == [{"type": "rich", "title": "T", "url": "http://x"}]


@pytest.mark.asyncio
async def test_empty_result_when_no_sources():
    guild = _make_guild()
    tool, _ = _make_setup(guilds=[guild])
    data = json.loads(await tool.execute())
    assert data["messages"] == []
    assert data["sources"] == {"scanned": 0, "skipped": 0}
    assert data["truncated"] is False


# ---------------------------------------------------------------------------
# DiscordChannel accessor + helper
# ---------------------------------------------------------------------------


def test_discord_channel_client_accessor_read_only():
    ch = DiscordChannel(DiscordConfig(), MessageBus())
    assert ch.client is None
    fake = MagicMock()
    ch._client = fake
    assert ch.client is fake


def test_is_channel_allowed_respects_allowlist():
    ch = DiscordChannel(DiscordConfig(allow_channels=["10"]), MessageBus())
    listed = SimpleNamespace(id=10, parent_id=None, parent=None)
    unlisted = SimpleNamespace(id=20, parent_id=None, parent=None)
    assert ch.is_channel_allowed(listed) is True
    assert ch.is_channel_allowed(unlisted) is False


def test_is_channel_allowed_empty_allowlist_allows_all():
    ch = DiscordChannel(DiscordConfig(), MessageBus())
    src = SimpleNamespace(id=999, parent_id=None, parent=None)
    assert ch.is_channel_allowed(src) is True
