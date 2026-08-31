"""Tests for the memory_read / memory_write tools (curated topic memory)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.memory_read import MemoryReadTool
from nanobot.agent.tools.memory_write import MemoryWriteTool


def _write_history(workspace: Path, rows: list[dict]) -> Path:
    """Append raw JSONL records (tools must never write history themselves)."""
    mem = workspace / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    path = mem / "history.jsonl"
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


def _entry(cursor: int, ts: str, *, session_key: str = "telegram:1", **fields) -> dict:
    return {"cursor": cursor, "timestamp": ts, "session_key": session_key, **fields}


def _write_topic(mem: Path, rel: str, *, title: str = "", description: str = "",
                 updated: str = "", tags: list[str] | None = None, body: str = "") -> Path:
    """Write a topic file with optional frontmatter (bypasses the write tool)."""
    path = mem / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if title:
        lines.append(f"title: {title}")
    if description:
        lines.append(f"description: {description}")
    if updated:
        lines.append(f"updated: {updated}")
    if tags:
        lines.append("tags: [" + ", ".join(tags) + "]")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n\n" + body, encoding="utf-8")
    return path


def _read_tool(workspace: Path) -> MemoryReadTool:
    return MemoryReadTool(workspace=workspace)


def _write_tool(workspace: Path) -> MemoryWriteTool:
    return MemoryWriteTool(workspace=workspace)


def _snapshot(root: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = path.read_bytes()
    return snapshot


# ---------------------------------------------------------------------------
# memory_read: list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_discovers_topic_files_with_descriptions(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    _write_topic(mem, "homelab.md", title="Homelab", description="Home lab notes.",
                 updated="2026-01-02 03:04:05", tags=["homelab", "net"],
                 body="# Homelab\n- upgrade pihole\n")
    _write_topic(mem, "projects/roadmap.md", description="Project roadmap.",
                 body="# Roadmap\nRewrite the protocol parser.\n")
    (mem / "system").mkdir()
    (mem / "system" / "now.md").write_text("status: done\n", encoding="utf-8")
    (mem / "MEMORY.md").write_text("# Long-term Memory\n", encoding="utf-8")

    result = await _read_tool(tmp_path).execute("list")

    assert result.status == "success"
    files = result.data["files"]
    paths = [f["path"] for f in files]
    assert paths == ["memory/homelab.md", "memory/projects/roadmap.md"]
    # Descriptions come from frontmatter (or the first heading as fallback).
    homelab = next(f for f in files if f["path"] == "memory/homelab.md")
    assert homelab["title"] == "Homelab"
    assert homelab["description"] == "Home lab notes."
    assert homelab["updated"] == "2026-01-02 03:04:05"
    assert homelab["tags"] == ["homelab", "net"]
    assert homelab["lines"] >= 6
    roadmap = next(f for f in files if f["path"] == "memory/projects/roadmap.md")
    assert roadmap["description"] == "Project roadmap."
    # memory/system/ and MEMORY.md are always loaded / excluded from the surface.
    assert not any("system" in f["path"] for f in files)
    assert not any(f["path"] == "memory/MEMORY.md" for f in files)
    # The list output shows descriptions before any body content.
    assert "Home lab notes." in str(result)


# ---------------------------------------------------------------------------
# memory_read: read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_returns_frontmatter_and_body(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    _write_topic(mem, "homelab.md", title="Homelab", description="Home lab notes.",
                 updated="2026-01-02 03:04:05", tags=["homelab"],
                 body="# Homelab\n\n- upgrade pihole\n- settle the eta protocol\n")

    result = await _read_tool(tmp_path).execute("read", path="homelab.md")

    assert result.status == "success"
    data = result.data
    assert data["action"] == "read"
    assert data["path"] == "memory/homelab.md"
    assert data["title"] == "Homelab"
    assert data["description"] == "Home lab notes."
    assert data["updated"] == "2026-01-02 03:04:05"
    assert data["tags"] == ["homelab"]
    assert "- upgrade pihole" in data["body"]
    assert "rewrite protocol" not in data["body"]
    # Full body is part of the human-readable output as well.
    assert "- settle the eta protocol" in str(result)


@pytest.mark.asyncio
async def test_read_workspace_relative_alias_and_nested_topic(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    _write_topic(mem, "projects/roadmap.md", description="Project roadmap.",
                 body="# Roadmap\nRewrite the protocol parser.\n")

    result = await _read_tool(tmp_path).execute("read", path="memory/projects/roadmap.md")

    assert result.status == "success"
    assert result.data["path"] == "memory/projects/roadmap.md"
    assert "Rewrite the protocol parser." in result.data["body"]


@pytest.mark.asyncio
async def test_read_missing_file_is_retryable(tmp_path: Path) -> None:
    (tmp_path / "memory").mkdir()
    result = await _read_tool(tmp_path).execute("read", path="nope.md")
    assert result.status == "retryable_error"
    assert "no such topic memory file" in str(result)
    assert "action=list" in str(result)


@pytest.mark.asyncio
async def test_read_requires_path(tmp_path: Path) -> None:
    (tmp_path / "memory").mkdir()
    result = await _read_tool(tmp_path).execute("read")
    assert result.status == "retryable_error"
    assert "requires 'path'" in str(result)


# ---------------------------------------------------------------------------
# memory_read: literal search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_finds_topic_files_and_history_summaries_messages(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(1, "2025-06-16T10:30:00+00:00",
               summary="rewrite protocol summary hit",
               messages=[{"role": "user", "content": "unrelated message"}]),
        _entry(2, "2025-06-15T09:00:00+00:00",
               messages=[{"role": "user", "content": "rewrite protocol message hit"}]),
        _entry(3, "2025-06-14T08:00:00+00:00", content="rewrite protocol legacy hit"),
    ])
    mem = tmp_path / "memory"
    (mem / "system").mkdir()
    (mem / "system" / "now.md").write_text("rewrite protocol system line\n", encoding="utf-8")
    _write_topic(mem, "notes.md", description="notes",
                 body="the rewrite protocol module lives in nanobot/agent.\n")
    _write_topic(mem, "unrelated.md", description="other", body="nothing here.\n")

    result = await _read_tool(tmp_path).execute("search", query="rewrite protocol")

    assert result.status == "success"
    kinds = [m["kind"] for m in result.data["history"]]
    assert "summary" in kinds
    assert "message" in kinds
    assert "content" in kinds
    assert result.data["history_total_matches"] == 3
    summary = next(m for m in result.data["history"] if m["kind"] == "summary")
    assert summary["cursor"] == 1
    assert summary["session_key"] == "telegram:1"
    msg = next(m for m in result.data["history"] if m["kind"] == "message")
    assert msg["role"] == "user"
    memory_paths = [f["path"] for f in result.data["memory_files"]]
    assert "memory/notes.md" in memory_paths
    # memory/system/ is excluded from the file surface.
    assert not any("system" in p for p in memory_paths)
    notes = next(f for f in result.data["memory_files"] if f["path"] == "memory/notes.md")
    assert any("nanobot/agent" in m["text"] for m in notes["matches"])


@pytest.mark.asyncio
async def test_search_flags_and_caps(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(i, f"2025-06-{i:02d}T08:00:00+00:00", content=f"eta protocol record {i}")
        for i in range(1, 8)
    ])
    result = await _read_tool(tmp_path).execute("search", query="eta protocol", max_results=3)
    assert result.status == "success"
    assert len(result.data["history"]) == 3
    assert result.data["history_total_matches"] == 7
    assert result.data["history_truncated"] is True
    assert "of 7 history matches" in str(result)

    result = await _read_tool(tmp_path).execute("search", query="nope")
    assert result.status == "success"
    assert result.data["history_total_matches"] == 0

    result = await _read_tool(tmp_path).execute(
        "search", query="eta protocol", search_history=False, search_memory_files=False
    )
    assert result.status == "retryable_error"
    assert "nothing to search" in str(result)


@pytest.mark.asyncio
async def test_search_requires_query(tmp_path: Path) -> None:
    (tmp_path / "memory").mkdir()
    result = await _read_tool(tmp_path).execute("search", query="   ")
    assert result.status == "retryable_error"
    assert "query" in str(result)

    result = await _read_tool(tmp_path).execute("search", search_history=False)
    assert result.status == "retryable_error"


# ---------------------------------------------------------------------------
# memory_write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_creates_file_with_frontmatter(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    tool = _write_tool(tmp_path)

    result = await tool.execute(
        path="homelab.md",
        title="Homelab notes",
        description="Home lab setup notes.",
        tags=["homelab", "network"],
        body="# Homelab\n\n- upgrade pihole\n",
    )

    assert result.status == "success"
    assert result.postcondition == "checked"
    assert result.side_effects == [
        {"kind": "topic_memory_write", "path": "memory/homelab.md", "created": True}
    ]
    assert result.data == {
        "action": "write",
        "path": "memory/homelab.md",
        "created": True,
        "title": "Homelab notes",
        "description": "Home lab setup notes.",
        "updated": result.data["updated"],
        "tags": ["homelab", "network"],
        "body_bytes": result.data["body_bytes"],
    }
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", result.data["updated"])

    content = (mem / "homelab.md").read_text(encoding="utf-8")
    assert content.startswith("---\ntitle: Homelab notes\n")
    assert "description: Home lab setup notes." in content
    assert "updated: " in content
    assert "tags: [homelab, network]" in content
    assert "# Homelab" in content
    assert "history.jsonl" not in content
    # Write must not create history/cursor artifacts.
    assert not (mem / "history.jsonl").exists()
    assert not (mem / ".cursor").exists()
    assert not (mem / ".dream_cursor").exists()


@pytest.mark.asyncio
async def test_write_creates_nested_topic(tmp_path: Path) -> None:
    result = await _write_tool(tmp_path).execute(
        path="projects/roadmap.md", body="# Roadmap\nRewrite the parser.\n"
    )
    assert result.status == "success"
    assert result.data["created"] is True
    target = tmp_path / "memory" / "projects" / "roadmap.md"
    assert target.exists()
    assert "title: roadmap" in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_write_update_preserves_unpassed_frontmatter_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    _clock = [0]

    def _fake_now_updated() -> str:
        _clock[0] += 1
        return f"2026-06-24 10:00:{_clock[0]:02d}"

    monkeypatch.setattr("nanobot.agent.tools.memory_write.now_updated", _fake_now_updated)
    tool = _write_tool(tmp_path)

    first = await tool.execute(
        path="homelab.md",
        title="Homelab v1",
        description="Original description.",
        tags=["homelab"],
        body="First body.\n",
    )
    assert first.data["created"] is True
    first_updated = first.data["updated"]

    second = await tool.execute(path="homelab.md", body="Second body, nothing else.\n")

    assert second.status == "success"
    assert second.data["created"] is False
    # Fields not passed are preserved from the existing frontmatter.
    assert second.data["title"] == "Homelab v1"
    assert second.data["description"] == "Original description."
    assert second.data["tags"] == ["homelab"]
    # The updated timestamp is refreshed on every write.
    assert second.data["updated"] != first_updated

    content = (mem / "homelab.md").read_text(encoding="utf-8")
    assert "title: Homelab v1" in content
    assert "description: Original description." in content
    assert "tags: [homelab]" in content
    assert "Second body" in content
    assert "First body" not in content


@pytest.mark.asyncio
async def test_write_derive_defaults_on_create(tmp_path: Path) -> None:
    result = await _write_tool(tmp_path).execute(path="fresh.md", body="First line of body.\n# Heading\n")
    assert result.status == "success"
    assert result.data["created"] is True
    assert result.data["title"] == "fresh"
    assert result.data["description"] == "First line of body."


@pytest.mark.asyncio
async def test_write_requires_nonempty_body(tmp_path: Path) -> None:
    tool = _write_tool(tmp_path)
    result = await tool.execute(path="x.md", body="   ")
    assert result.status == "retryable_error"
    assert "body" in str(result)
    result = await tool.execute(path="x.md", body=None)  # type: ignore[arg-type]
    assert result.status == "retryable_error"


@pytest.mark.asyncio
async def test_write_atomic_leaves_no_tmp_files(tmp_path: Path) -> None:
    tool = _write_tool(tmp_path)
    for i in range(5):
        result = await tool.execute(path=f"topic{i}.md", body=f"Body {i}.\n")
        assert result.status == "success"
    # Update a few existing files as well.
    for i in range(2):
        result = await tool.execute(path=f"topic{i}.md", body=f"Updated {i}.\n")
        assert result.status == "success"

    files = sorted(p.name for p in (tmp_path / "memory").rglob("*") if p.is_file())
    assert files == ["topic0.md", "topic1.md", "topic2.md", "topic3.md", "topic4.md"]
    assert not any(".tmp" in name or name.startswith(".") for name in files)


# ---------------------------------------------------------------------------
# path safety for both tools
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_path", [
    "../../etc/passwd",
    "/etc/passwd",
    "memory/../outside.md",
    "..",
])
@pytest.mark.asyncio
async def test_escape_paths_rejected_by_both_tools(tmp_path: Path, bad_path: str) -> None:
    before = _snapshot(tmp_path)
    read_result = await _read_tool(tmp_path).execute("read", path=bad_path)
    write_result = await _write_tool(tmp_path).execute(path=bad_path, body="x")
    for result in (read_result, write_result):
        assert result.status == "policy_block"
        msg = str(result).lower()
        assert any(needle in msg for needle in ("memory", "relative", "segment"))
        assert result.retryable is False
    # Nothing was created outside or inside memory.
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("bad_path", [
    "history.jsonl",
    ".dream_cursor",
    ".cursor",
    "MEMORY.md",
])
@pytest.mark.asyncio
async def test_protected_files_rejected_by_both_tools(tmp_path: Path, bad_path: str) -> None:
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "history.jsonl").write_text("{}\n", encoding="utf-8")
    (mem / ".dream_cursor").write_text("0", encoding="utf-8")
    before = _snapshot(tmp_path)

    read_result = await _read_tool(tmp_path).execute("read", path=bad_path)
    write_result = await _write_tool(tmp_path).execute(path=bad_path, body="x")
    for result in (read_result, write_result):
        assert result.status == "policy_block"
        assert "protected" in str(result).lower() or "not a topic" in str(result).lower()
    assert _snapshot(tmp_path) == before


@pytest.mark.asyncio
async def test_system_dir_rejected_by_both_tools(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    (mem / "system").mkdir(parents=True)
    (mem / "system" / "now.md").write_text("status: fine\n", encoding="utf-8")
    before = _snapshot(tmp_path)

    for bad_path in ("system/now.md", "memory/system/now.md"):
        read_result = await _read_tool(tmp_path).execute("read", path=bad_path)
        write_result = await _write_tool(tmp_path).execute(path=bad_path, body="x")
        for result in (read_result, write_result):
            assert result.status == "policy_block"
    assert _snapshot(tmp_path) == before
    assert (mem / "system" / "now.md").read_text(encoding="utf-8") == "status: fine\n"


# ---------------------------------------------------------------------------
# read-only guarantee
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_read_never_writes_anything(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(1, "2025-06-16T10:30:00+00:00", summary="xi protocol summary"),
    ])
    mem = tmp_path / "memory"
    _write_topic(mem, "topic.md", title="Topic", description="A topic.",
                 body="xi protocol line\n")
    (mem / "system").mkdir()
    (mem / "system" / "now.md").write_text("xi protocol system line\n", encoding="utf-8")
    before = _snapshot(tmp_path)

    tool = _read_tool(tmp_path)
    for action in ("list", "read", "search"):
        if action == "read":
            result = await tool.execute(action, path="topic.md")
        elif action == "search":
            result = await tool.execute(action, query="xi protocol")
        else:
            result = await tool.execute(action)
        assert result.status == "success"

    assert _snapshot(tmp_path) == before
    # No cursor/history/tmp artifacts created by reading.
    assert not (mem / ".cursor").exists()
    assert not (mem / ".dream_cursor").exists()
    assert not any(".tmp" in p.name for p in mem.rglob("*"))


# ---------------------------------------------------------------------------
# schema / discovery plumbing
# ---------------------------------------------------------------------------


def test_both_tools_discoverable_and_flags() -> None:
    loader = ToolLoader()
    class_names = {cls.__name__ for cls in loader.discover()}
    assert "MemoryReadTool" in class_names
    assert "MemoryWriteTool" in class_names

    read_tool = MemoryReadTool(workspace=Path("."))
    assert read_tool.name == "memory_read"
    assert read_tool.read_only is True
    assert read_tool.concurrency_safe is True

    write_tool = MemoryWriteTool(workspace=Path("."))
    assert write_tool.name == "memory_write"
    assert write_tool.read_only is False
    assert write_tool.concurrency_safe is False

    for tool in (read_tool, write_tool):
        assert tool._scopes == {"core", "subagent", "memory"}


def test_memory_read_schema_validation() -> None:
    tool = MemoryReadTool(workspace=Path("."))
    schema = tool.parameters
    assert schema["required"] == ["action"]
    assert schema["properties"]["action"]["enum"] == ["list", "read", "search"]
    assert tool.validate_params({"action": "list"}) == []
    assert tool.validate_params({"action": "bogus"}) != []          # bad enum
    assert tool.validate_params({"action": "search", "query": "x"}) == []
    # action=search without query passes schema validation; the query is
    # required at execution time (JSON Schema cannot express conditional required).
    assert tool.validate_params({"action": "search"}) == []


def test_memory_write_schema_validation() -> None:
    tool = MemoryWriteTool(workspace=Path("."))
    schema = tool.parameters
    assert schema["required"] == ["path", "body"]
    assert tool.validate_params({"path": "x.md", "body": "b"}) == []
    assert tool.validate_params({"path": "x.md"}) != []            # body required
    assert tool.validate_params({"body": "b"}) != []               # path required
    assert tool.validate_params({"path": "x.md", "body": "b", "tags": ["a", 1]}) != []
    assert tool.validate_params({"path": "x.md", "body": "b", "spurious": 1}) != []


@pytest.mark.asyncio
async def test_write_result_carries_structured_metadata(tmp_path: Path) -> None:
    result = await _write_tool(tmp_path).execute(
        path="meta.md", body="Structured metadata.\n"
    )
    assert isinstance(result, ToolResult)
    assert result.status == "success"
    assert result.postcondition == "checked"
    assert result.data["path"] == "memory/meta.md"
    assert result.side_effects[0]["kind"] == "topic_memory_write"
    assert result.evidence[0]["kind"] == "memory_file_write"
