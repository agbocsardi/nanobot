"""Tests for the /remember built-in command (targeted user memory writes)."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.bus.events import InboundMessage
from nanobot.command.builtin import (
    BUILTIN_COMMAND_SPECS,
    DEFAULT_REMEMBER_TOPIC,
    cmd_remember,
    register_builtin_commands,
)
from nanobot.command.router import CommandContext, CommandRouter


def _ctx(tmp_path: Path, raw: str, args: str = "") -> CommandContext:
    """Minimal CommandContext whose loop exposes a real workspace."""
    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="direct", content=raw)
    loop = SimpleNamespace(workspace=tmp_path)
    return CommandContext(msg=msg, session=None, key=msg.session_key, raw=raw, args=args, loop=loop)


def _read_topic(tmp_path: Path, rel: str) -> str:
    path = tmp_path / "memory" / rel
    assert path.exists(), f"memory/{rel} was not created"
    return path.read_text(encoding="utf-8")


def _write_topic(tmp_path: Path, rel: str, *, title: str = "Old", description: str = "Old desc",
                 body: str) -> None:
    path = tmp_path / "memory" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\ndescription: {description}\nupdated: 2026-01-01 00:00:00\n---\n\n{body}",
        encoding="utf-8",
    )


async def _remember(tmp_path: Path, args: str) -> str:
    out = await cmd_remember(_ctx(tmp_path, "/remember", args=args))
    assert out is not None
    return out.content


@pytest.mark.asyncio
async def test_remember_creates_file_with_frontmatter(tmp_path: Path) -> None:
    content = await _remember(tmp_path, "the user prefers dark mode")

    assert f"memory/{DEFAULT_REMEMBER_TOPIC}" in content
    saved = _read_topic(tmp_path, DEFAULT_REMEMBER_TOPIC)
    assert saved.startswith("---\n")
    assert re.search(r"^title: ", saved, re.MULTILINE)
    assert re.search(r"^description: ", saved, re.MULTILINE)
    assert re.search(r"^updated: ", saved, re.MULTILINE)
    assert "the user prefers dark mode" in saved
    # Notes land under a dated heading, never as raw unanchored prose.
    assert re.search(r"^## \d{4}-\d{2}-\d{2}$", saved, re.MULTILINE)


@pytest.mark.asyncio
async def test_remember_appends_preserving_prior_content(tmp_path: Path) -> None:
    _write_topic(tmp_path, DEFAULT_REMEMBER_TOPIC, body="first note line\n")

    content = await _remember(tmp_path, "second note line")
    saved = _read_topic(tmp_path, DEFAULT_REMEMBER_TOPIC)

    assert "appended" in content
    assert saved.index("first note line") < saved.index("second note line")
    assert saved.count("## ") >= 1
    # Prior frontmatter survives and keeps its title/description.
    assert "title: Old" in saved
    assert "description: Old desc" in saved
    assert saved.count("second note line") == 1
    assert saved.count("first note line") == 1


@pytest.mark.asyncio
async def test_remember_topic_variant(tmp_path: Path) -> None:
    content = await _remember(tmp_path, "projects: fix the flaky test")

    assert "memory/projects.md" in content
    saved = _read_topic(tmp_path, "projects.md")
    assert "fix the flaky test" in saved
    assert re.search(r"^title: projects$", saved, re.MULTILINE)
    # Default topic is untouched.
    assert not (tmp_path / "memory" / DEFAULT_REMEMBER_TOPIC).exists()


@pytest.mark.asyncio
async def test_remember_path_like_input_never_escapes(tmp_path: Path) -> None:
    """Non-slug prefixes fall back to the default topic as plain text."""
    content = await _remember(tmp_path, "../../etc/passwd: add user")

    assert "memory/user-notes.md" in content
    saved = _read_topic(tmp_path, DEFAULT_REMEMBER_TOPIC)
    assert "../../etc/passwd: add user" in saved
    assert not (tmp_path / "etc").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["memory"]


@pytest.mark.asyncio
async def test_remember_protected_names_fall_back_to_default_topic(tmp_path: Path) -> None:
    """history.jsonl/.dream_cursor contain dots; the one-rule parser treats
    them as plain notes, so the protected files are never written."""
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory" / "history.jsonl").write_text("{}", encoding="utf-8")
    (tmp_path / "memory" / ".dream_cursor").write_text("0", encoding="utf-8")

    for topic in ("history.jsonl", ".dream_cursor"):
        content = await _remember(tmp_path, f"{topic}: secret")

        assert "memory/user-notes.md" in content
        saved = _read_topic(tmp_path, DEFAULT_REMEMBER_TOPIC)
        assert f"{topic}: secret" in saved
        assert (tmp_path / "memory" / topic).read_text(encoding="utf-8") in ("{}", "0")


@pytest.mark.asyncio
async def test_remember_path_like_arguments_fall_back_to_default_topic(tmp_path: Path) -> None:
    """Path-like prefixes are prose, not topics: note lands in user-notes.md."""
    (tmp_path / "memory" / "system").mkdir(parents=True, exist_ok=True)

    for args in ("mem/ory: split path", "system/boot: boot note", ": empty topic"):
        content = await _remember(tmp_path, args)
        assert "memory/user-notes.md" in content, f"expected default topic for {args!r}"
        saved = _read_topic(tmp_path, DEFAULT_REMEMBER_TOPIC)
        assert args in saved
    assert sorted(p.name for p in (tmp_path / "memory").iterdir()) == ["system", "user-notes.md"]


@pytest.mark.asyncio
async def test_remember_prose_prefix_falls_back_to_default_topic(tmp_path: Path) -> None:
    content = await _remember(tmp_path, "note to self: buy milk")
    assert "memory/user-notes.md" in content
    saved = _read_topic(tmp_path, DEFAULT_REMEMBER_TOPIC)
    assert "note to self: buy milk" in saved


@pytest.mark.asyncio
async def test_remember_time_prefix_falls_back_to_default_topic(tmp_path: Path) -> None:
    content = await _remember(tmp_path, "at 20:00 call bob")
    assert "memory/user-notes.md" in content
    saved = _read_topic(tmp_path, DEFAULT_REMEMBER_TOPIC)
    assert "at 20:00 call bob" in saved
    assert not (tmp_path / "memory" / "at20.md").exists()


@pytest.mark.asyncio
async def test_remember_rejects_directory_topic(tmp_path: Path) -> None:
    (tmp_path / "memory" / "somedir.md").mkdir(parents=True, exist_ok=True)
    content = await _remember(tmp_path, "somedir: note")
    assert "Error" in content
    assert "is a directory" in content


@pytest.mark.asyncio
async def test_remember_usage_when_no_args(tmp_path: Path) -> None:
    content = await _remember(tmp_path, "")
    assert "Usage: /remember" in content
    assert not (tmp_path / "memory").exists()


@pytest.mark.asyncio
async def test_remember_word_prefix_is_explicit_topic(tmp_path: Path) -> None:
    """A slug before the colon selects the topic — deterministic colon syntax."""
    content = await _remember(tmp_path, "Remember: buy milk")
    assert "memory/remember.md" in content
    saved = _read_topic(tmp_path, "remember.md")
    assert "buy milk" in saved
    assert not (tmp_path / "memory" / DEFAULT_REMEMBER_TOPIC).exists()


@pytest.mark.asyncio
async def test_remember_system_md_file_is_topic_like_memory_write(tmp_path: Path) -> None:
    """system.md (a file) is a topic; only memory/system/* is protected."""
    content = await _remember(tmp_path, "system: boot note")
    assert "memory/system.md" in content
    saved = _read_topic(tmp_path, "system.md")
    assert "boot note" in saved


@pytest.mark.asyncio
async def test_remember_registered_and_in_specs(tmp_path: Path) -> None:
    specs = {spec.command: spec for spec in BUILTIN_COMMAND_SPECS}
    assert "/remember" in specs
    assert specs["/remember"].arg_hint == "[<topic>:] <text>"

    router = CommandRouter()
    register_builtin_commands(router)
    out = await router.dispatch(_ctx(tmp_path, "/remember hello", args="hello"))
    assert out is not None
    assert "memory/user-notes.md" in out.content
