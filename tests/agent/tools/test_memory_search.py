"""Regression tests for the built-in memory_search tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.memory_search import MemorySearchTool

V3_MESSAGES = [
    {
        "timestamp": "2025-06-16T10:29:00+00:00",
        "role": "user",
        "content": "How do I rewrite the protocol parser?",
    },
    {
        "timestamp": "2025-06-16T10:30:00+00:00",
        "role": "assistant",
        "content": "The rewrite protocol module lives in nanobot/agent.",
    },
]


def _write_history(workspace: Path, rows: list[dict]) -> Path:
    """Append raw JSONL records, bypassing MemoryStore writes (tool must stay read-only)."""
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


def _tool(workspace: Path) -> MemorySearchTool:
    return MemorySearchTool(workspace=workspace)


async def _run(workspace: Path, query: str, **kwargs) -> ToolResult:
    return await _tool(workspace).execute(query, **kwargs)


# ---------------------------------------------------------------------------
# literal matching across history shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_literal_search_hits_v3_summaries_and_messages(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(
            1,
            "2025-06-16T10:30:00+00:00",
            summary="User asked about rewriting the protocol parser; assistant pointed at the rewrite protocol module.",
            messages=V3_MESSAGES,
        ),
        _entry(
            2,
            "2025-06-15T09:12:00+00:00",
            messages=[{
                "timestamp": "2025-06-15T09:11:00+00:00",
                "role": "user",
                "content": "the rewrite protocol module needs docs",
            }],
        ),
    ])

    result = await _run(tmp_path, "rewrite protocol")

    assert result.status == "success"
    kinds = [m["kind"] for m in result.data["history"]]
    assert "summary" in kinds
    assert "message" in kinds
    summary = next(m for m in result.data["history"] if m["kind"] == "summary")
    assert summary["cursor"] == 1
    assert summary["session_key"] == "telegram:1"
    assert "rewrite protocol module" in summary["excerpt"]
    msg = next(m for m in result.data["history"] if m["kind"] == "message")
    assert msg["role"] == "user"
    assert msg["cursor_id"] == "cursor=2 msg=1"
    assert "rewrite protocol module needs docs" in msg["excerpt"]


@pytest.mark.asyncio
async def test_summary_takes_precedence_over_messages(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(
            1,
            "2025-06-16T10:30:00+00:00",
            summary="summary about the alpha protocol",
            messages=[{
                "timestamp": "2025-06-16T10:29:00+00:00",
                "role": "user",
                "content": "alpha protocol question",
            }],
        ),
    ])

    result = await _run(tmp_path, "alpha protocol")

    # One compact result, summary wins over the matching message.
    assert [m["kind"] for m in result.data["history"]] == ["summary"]
    assert result.data["history_total_matches"] == 1


@pytest.mark.asyncio
async def test_legacy_content_fallback(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(3, "2025-06-14 08:00", session_key="dream:1",
               content="legacy entry mentioning the rewrite protocol plan"),
    ])

    result = await _run(tmp_path, "rewrite protocol")

    assert len(result.data["history"]) == 1
    match = result.data["history"][0]
    assert match["kind"] == "content"
    assert match["cursor"] == 3
    assert "rewrite protocol plan" in match["excerpt"]
    assert "legacy entry" in str(result)


@pytest.mark.asyncio
async def test_line_id_fallback_when_cursor_missing(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        {"timestamp": "2025-06-14 08:00", "content": "orphan row about the beta protocol"},
    ])

    result = await _run(tmp_path, "beta protocol")

    match = result.data["history"][0]
    assert match["cursor"] is None
    assert match["line"] == 1
    assert match["cursor_id"] == "line=1"


@pytest.mark.asyncio
async def test_no_match_is_success_not_error(tmp_path: Path) -> None:
    _write_history(tmp_path, [_entry(1, "2025-06-16T10:30:00+00:00", content="nothing here")])

    result = await _run(tmp_path, "NoSuchTerm")

    assert result.status == "success"
    assert result.data["history_total_matches"] == 0
    assert "no history matches" in str(result)


# ---------------------------------------------------------------------------
# structured filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_key_filter(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(1, "2025-06-16T10:30:00+00:00", session_key="telegram:1", content="gamma protocol in telegram"),
        _entry(2, "2025-06-16T11:00:00+00:00", session_key="discord:9", content="gamma protocol in discord"),
    ])

    result = await _run(tmp_path, "gamma protocol", session_key="discord:9")

    assert [m["cursor"] for m in result.data["history"]] == [2]
    assert "session=discord:9" in str(result)


@pytest.mark.asyncio
async def test_role_filter_restricts_to_structured_messages(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(
            1,
            "2025-06-16T10:30:00+00:00",
            summary="summary mentioning delta protocol",
            messages=[
                {"timestamp": "2025-06-16T10:29:00+00:00", "role": "user", "content": "delta protocol setup?"},
                {"timestamp": "2025-06-16T10:30:00+00:00", "role": "assistant", "content": "delta protocol answer"},
            ],
        ),
        _entry(2, "2025-06-15T09:00:00+00:00", content="legacy delta protocol content"),
    ])

    result = await _run(tmp_path, "delta protocol", role="assistant")

    matches = result.data["history"]
    assert [m["kind"] for m in matches] == ["message"]
    assert all(m["role"] == "assistant" for m in matches)
    # Legacy content has no role and must be excluded when role is set.
    assert all(m["cursor"] != 2 for m in matches)


@pytest.mark.asyncio
async def test_date_range_filter(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(1, "2025-06-10T08:00:00+00:00", content="epsilon protocol early"),
        _entry(2, "2025-06-15T08:00:00+00:00", content="epsilon protocol mid"),
        _entry(3, "2025-06-20T08:00:00+00:00", content="epsilon protocol late"),
    ])

    result = await _run(tmp_path, "epsilon protocol", date_from="2025-06-12", date_to="2025-06-18")

    assert [m["cursor"] for m in result.data["history"]] == [2]


@pytest.mark.asyncio
async def test_date_from_inclusive_at_day_boundary(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(1, "2025-06-16 00:00", content="boundary protocol at midnight"),
    ])

    result = await _run(tmp_path, "boundary protocol", date_from="2025-06-16")

    assert [m["cursor"] for m in result.data["history"]] == [1]


@pytest.mark.asyncio
async def test_invalid_or_inverted_date_filters_are_retryable(tmp_path: Path) -> None:
    _write_history(tmp_path, [_entry(1, "2025-06-16T10:30:00+00:00", content="zeta protocol")])

    result = await _run(tmp_path, "zeta protocol", date_from="not-a-date")
    assert result.status == "retryable_error"
    assert "date_from" in str(result)

    result = await _run(tmp_path, "zeta protocol", date_from="2025-06-20", date_to="2025-06-10")
    assert result.status == "retryable_error"
    assert "after date_to" in str(result)


# ---------------------------------------------------------------------------
# topic memory file search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_file_search_returns_paths_and_lines(tmp_path: Path) -> None:
    _write_history(tmp_path, [])
    mem = tmp_path / "memory"
    (mem / "homelab.md").write_text(
        '---\ndescription: "Home lab notes"\n---\n\n# Homelab\n- settle the eta protocol on the NAS\n- upgrade pihole\n',
        encoding="utf-8",
    )
    (mem / "system").mkdir()
    (mem / "system" / "now.md").write_text("eta protocol status: done\n", encoding="utf-8")

    result = await _run(tmp_path, "eta protocol")

    files = result.data["memory_files"]
    paths = [f["path"] for f in files]
    assert "memory/homelab.md" in paths
    # memory/system/ is already loaded into context in full; excluded from search.
    assert not any("system" in p for p in paths)
    homelab = next(f for f in files if f["path"] == "memory/homelab.md")
    assert [m["line"] for m in homelab["matches"]] == [6]
    assert "settle the eta protocol" in homelab["matches"][0]["text"]
    # Frontmatter description line ("Home lab notes") must not be a match source.
    assert all("Home lab" not in m["text"] for m in homelab["matches"])


@pytest.mark.asyncio
async def test_search_memory_files_can_be_disabled(tmp_path: Path) -> None:
    _write_history(tmp_path, [_entry(1, "2025-06-16T10:30:00+00:00", content="theta protocol in history")])
    (tmp_path / "memory" / "notes.md").write_text("theta protocol in a topic file\n", encoding="utf-8")

    result = await _run(tmp_path, "theta protocol", search_memory_files=False)

    assert result.data["history_total_matches"] == 1
    assert result.data["memory_files"] == []
    assert "no memory file matches" in str(result)


# ---------------------------------------------------------------------------
# caps and safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_results_cap_respected(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(i, f"2025-06-{i:02d}T08:00:00+00:00", content=f"iota protocol record {i}")
        for i in range(1, 8)
    ])

    result = await _run(tmp_path, "iota protocol", max_results=3)

    assert len(result.data["history"]) == 3
    assert result.data["history_total_matches"] == 7
    assert result.data["history_truncated"] is True
    assert "of 7 history matches" in str(result)


@pytest.mark.asyncio
async def test_max_excerpt_chars_cap_respected_in_text_and_data(tmp_path: Path) -> None:
    long_content = "kappa protocol " + "x" * 500
    _write_history(tmp_path, [_entry(1, "2025-06-16T10:30:00+00:00", content=long_content)])

    result = await _run(tmp_path, "kappa protocol", max_excerpt_chars=80)

    excerpt = result.data["history"][0]["excerpt"]
    assert len(excerpt) <= 80
    assert len(long_content) not in (0, len(excerpt))  # no whole raw message leaked
    assert "x" * 500 not in str(result)


@pytest.mark.asyncio
async def test_history_results_sorted_newest_first(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(1, "2025-06-10T08:00:00+00:00", content="lambda protocol older"),
        _entry(2, "2025-06-20T08:00:00+00:00", content="lambda protocol newer"),
    ])

    result = await _run(tmp_path, "lambda protocol")

    assert [m["cursor"] for m in result.data["history"]] == [2, 1]


# ---------------------------------------------------------------------------
# robustness and read-only guarantee
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_and_corrupt_history_handled_gracefully(tmp_path: Path) -> None:
    # No history file at all: success, zero matches, memory search still works.
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "notes.md").write_text("mu protocol in a topic file\n", encoding="utf-8")
    result = await _run(tmp_path, "mu protocol")
    assert result.status == "success"
    assert result.data["history_total_matches"] == 0
    assert result.data["memory_files"]

    # Corrupt / non-dict / blank lines are skipped; valid entries still match.
    _write_history(tmp_path, [
        {"cursor": 10, "timestamp": "2025-06-16T10:30:00+00:00", "content": "nu protocol valid"},
    ])
    path = tmp_path / "memory" / "history.jsonl"
    path.write_text(
        '{"cursor": 10, "timestamp": "2025-06-16T10:30:00+00:00", "content": "nu protocol valid"}\n'
        "this is not json{{{}\n"
        "[1, 2, 3]\n"
        "\n"
        "{\"timestamp\": \"2025-06-16T11:00:00+00:00\", \"content\": \"nu protocol second\"}\n",
        encoding="utf-8",
    )
    result = await _run(tmp_path, "nu protocol")
    assert result.status == "success"
    assert result.data["history_total_matches"] == 2
    assert {m["cursor"] for m in result.data["history"]} == {10, None}


def _snapshot(workspace: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for path in sorted(workspace.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(workspace))] = path.read_bytes()
    return snapshot


@pytest.mark.asyncio
async def test_read_only_guarantee_files_unchanged(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(1, "2025-06-16T10:30:00+00:00", content="xi protocol history row"),
    ])
    (tmp_path / "memory" / "topic.md").write_text("xi protocol topic line\n", encoding="utf-8")
    (tmp_path / "memory" / "system").mkdir()
    (tmp_path / "memory" / "system" / "now.md").write_text("xi protocol system line\n", encoding="utf-8")
    before = _snapshot(tmp_path)

    result = await _run(tmp_path, "xi protocol", search_memory_files=True)

    assert result.status == "success"
    assert _snapshot(tmp_path) == before
    # The search must not create MemoryStore artifacts (cursor files, backups).
    assert not (tmp_path / "memory" / ".cursor").exists()
    assert not (tmp_path / "memory" / "HISTORY.md.bak").exists()


@pytest.mark.asyncio
async def test_empty_query_is_retryable_error(tmp_path: Path) -> None:
    _write_history(tmp_path, [])
    result = await _run(tmp_path, "   ")
    assert result.status == "retryable_error"
    assert "query must not be empty" in str(result)


# ---------------------------------------------------------------------------
# discovery / schema plumbing
# ---------------------------------------------------------------------------


def test_tool_discoverable_read_only_and_scoped() -> None:
    loader = ToolLoader()
    class_names = {cls.__name__ for cls in loader.discover()}
    assert "MemorySearchTool" in class_names

    tool = MemorySearchTool(workspace=Path("."))
    assert tool.name == "memory_search"
    assert tool.read_only is True
    assert tool.concurrency_safe is True
    assert tool._scopes == {"core", "subagent"}
    schema = tool.parameters
    assert schema["required"] == ["query"]
    assert schema["properties"]["query"]["minLength"] == 1
    for param in ("max_results", "max_excerpt_chars"):
        assert param in schema["properties"]
    assert tool.validate_params({"query": "x"}) == []
    assert tool.validate_params({"query": "x", "max_results": 999}) != []


@pytest.mark.asyncio
async def test_tool_result_carries_structured_data(tmp_path: Path) -> None:
    _write_history(tmp_path, [_entry(1, "2025-06-16T10:30:00+00:00", content="omicron protocol")])
    (tmp_path / "memory" / "notes.md").write_text("omicron protocol line\n", encoding="utf-8")

    result = await _run(tmp_path, "omicron protocol", case_sensitive=True)

    assert isinstance(result, ToolResult)
    assert result.data["query"] == "omicron protocol"
    assert result.data["case_sensitive"] is True
    assert result.data["history"][0]["kind"] == "content"
    assert result.data["memory_files"][0]["path"] == "memory/notes.md"
