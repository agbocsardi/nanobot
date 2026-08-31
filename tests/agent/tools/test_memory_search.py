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


def _tool(workspace: Path, **kwargs) -> MemorySearchTool:
    return MemorySearchTool(workspace=workspace, **kwargs)


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
    """history.jsonl and memory content files are byte-identical after a search.

    The only permitted new file is the disposable sidecar index
    memory/search_index.json; user data stays untouched.
    """
    _write_history(tmp_path, [
        _entry(1, "2025-06-16T10:30:00+00:00", content="xi protocol history row"),
    ])
    (tmp_path / "memory" / "topic.md").write_text("xi protocol topic line\n", encoding="utf-8")
    (tmp_path / "memory" / "system").mkdir()
    (tmp_path / "memory" / "system" / "now.md").write_text("xi protocol system line\n", encoding="utf-8")
    before = _snapshot(tmp_path)

    result = await _run(tmp_path, "xi protocol", search_memory_files=True)

    assert result.status == "success"
    after = _snapshot(tmp_path)
    for key in before:
        assert after[key] == before[key], key
    # The search must not create MemoryStore artifacts (cursor files, backups).
    assert not (tmp_path / "memory" / ".cursor").exists()
    assert not (tmp_path / "memory" / "HISTORY.md.bak").exists()
    # The only file the tool may create/update is the disposable index cache.
    assert set(after) - set(before) <= {"memory/search_index.json"}
    assert not (tmp_path / "memory" / "search_index.json.tmp").exists()


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


# ---------------------------------------------------------------------------
# v2: incremental history index
# ---------------------------------------------------------------------------


def _append_raw_history(workspace: Path, rows: list[dict]) -> Path:
    """Append raw JSONL records with a real file append (no head rewrite)."""
    path = workspace / "memory" / "history.jsonl"
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _read_index(workspace: Path) -> dict:
    return json.loads((workspace / "memory" / "search_index.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_index_built_from_scratch_on_first_search(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(1, "2025-06-16T10:30:00+00:00", content="rho protocol first"),
        _entry(2, "2025-06-17T10:30:00+00:00", content="rho protocol second"),
    ])
    assert not (tmp_path / "memory" / "search_index.json").exists()

    result = await _run(tmp_path, "rho protocol")

    assert result.data["history_total_matches"] == 2
    index = _read_index(tmp_path)
    assert index["version"] == 1
    assert index["size"] == (tmp_path / "memory" / "history.jsonl").stat().st_size
    assert [e["cursor"] for e in index["entries"]] == [1, 2]
    assert [e["offset"] for e in index["entries"]] == sorted(e["offset"] for e in index["entries"])


@pytest.mark.asyncio
async def test_index_appends_incrementally_without_reparsing_head(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(1, "2025-06-16T10:30:00+00:00", content="sigma protocol first"),
    ])
    await _run(tmp_path, "sigma protocol")  # first search builds the index
    first_index = _read_index(tmp_path)

    # Real append after the index was built (head bytes untouched).
    _append_raw_history(tmp_path, [
        _entry(2, "2025-06-17T10:30:00+00:00", content="sigma protocol second"),
        _entry(3, "2025-06-18T10:30:00+00:00", content="sigma protocol third"),
    ])

    history = tmp_path / "memory" / "history.jsonl"
    result = await _run(tmp_path, "sigma protocol")
    assert {m["cursor"] for m in result.data["history"]} == {1, 2, 3}

    index = _read_index(tmp_path)
    assert index["size"] == history.stat().st_size
    assert len(index["entries"]) == 3
    # Incremental update: the head entry was NOT re-scanned/re-built, only the
    # two appended rows were added on top of the untouched first entry.
    assert index["entries"][0] == first_index["entries"][0]
    assert [e["cursor"] for e in index["entries"]] == [1, 2, 3]
    assert [e["line"] for e in index["entries"]] == [1, 2, 3]


@pytest.mark.asyncio
async def test_index_based_search_still_sees_same_size_head_rewrites(tmp_path: Path) -> None:
    """Offsets index contents, so content rewrites at fixed offsets stay visible.

    The index only stores offsets/metadata; every search re-reads the entry
    bytes at the stored offset. A same-length rewrite therefore remains
    searchable (and re-scanning the head is what the index avoids — the head
    entry metadata is reused, not rebuilt).
    """
    _write_history(tmp_path, [
        _entry(1, "2025-06-16T10:30:00+00:00", content="sigma protocol first"),
        _entry(2, "2025-06-17T10:30:00+00:00", content="sigma protocol second"),
    ])
    await _run(tmp_path, "sigma protocol")
    first_index = _read_index(tmp_path)

    history = tmp_path / "memory" / "history.jsonl"
    lines = history.read_text(encoding="utf-8").splitlines()
    assert lines[0].count("first") == 1
    lines[0] = lines[0].replace("first", "firsy")  # same byte length, different text
    history.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = await _run(tmp_path, "firsy")
    assert result.data["history_total_matches"] == 1
    assert any("firsy" in m["excerpt"] for m in result.data["history"])

    index = _read_index(tmp_path)
    assert index["entries"][0] == first_index["entries"][0]  # head metadata never re-scanned


@pytest.mark.asyncio
async def test_index_rebuilds_on_truncation(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(1, "2025-06-16T10:30:00+00:00", content="tau protocol one"),
        _entry(2, "2025-06-17T10:30:00+00:00", content="tau protocol two"),
        _entry(3, "2025-06-18T10:30:00+00:00", content="tau protocol three"),
    ])
    await _run(tmp_path, "tau protocol")
    assert len(_read_index(tmp_path)["entries"]) == 3

    # Truncate: fewer bytes than indexed -> the index must rebuild from scratch.
    _write_history(tmp_path, [
        _entry(1, "2025-06-16T10:30:00+00:00", content="tau protocol one"),
    ])
    result = await _run(tmp_path, "tau protocol")

    assert [m["cursor"] for m in result.data["history"]] == [1]
    index = _read_index(tmp_path)
    assert len(index["entries"]) == 1
    assert index["size"] < (tmp_path / "memory" / "history.jsonl").stat().st_size + 1

    # And incremental appends work again after the rebuild.
    _append_raw_history(tmp_path, [_entry(2, "2025-06-19T10:30:00+00:00", content="tau protocol two")])
    result = await _run(tmp_path, "tau protocol")
    assert [m["cursor"] for m in result.data["history"]] == [2, 1]


@pytest.mark.asyncio
async def test_index_rebuilds_on_corrupt_index_file(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(1, "2025-06-16T10:30:00+00:00", content="upsilon protocol"),
    ])
    await _run(tmp_path, "upsilon protocol")
    index_file = tmp_path / "memory" / "search_index.json"
    index_file.write_text("{not json!!!", encoding="utf-8")

    result = await _run(tmp_path, "upsilon protocol")

    assert result.data["history_total_matches"] == 1
    index = _read_index(tmp_path)
    assert index["version"] == 1
    assert len(index["entries"]) == 1


@pytest.mark.asyncio
async def test_index_resume_line_number_after_corrupt_head(tmp_path: Path) -> None:
    """Appended entries after a corrupt/blank head keep correct physical line citations."""
    history = tmp_path / "memory" / "history.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text(
        '{"timestamp": "2025-06-16T10:30:00+00:00", "content": "digamma protocol one"}\n'
        "this is not json{{{}\n"
        "\n"
        '{"timestamp": "2025-06-16T11:00:00+00:00", "content": "digamma protocol four"}\n',
        encoding="utf-8",
    )
    result = await _run(tmp_path, "digamma protocol")
    assert [m["line"] for m in result.data["history"]] == [4, 1]

    # Real append: the new row is physical line 5 and must be cited as such.
    _append_raw_history(tmp_path, [
        {"timestamp": "2025-06-16T12:00:00+00:00", "content": "digamma protocol five"},
    ])
    result = await _run(tmp_path, "digamma protocol")
    by_line = {m["line"]: m["excerpt"] for m in result.data["history"]}
    assert set(by_line) == {1, 4, 5}
    assert "five" in by_line[5]
    index = _read_index(tmp_path)
    assert index["line"] == 5
    assert [e["line"] for e in index["entries"]] == [1, 4, 5]


@pytest.mark.asyncio
async def test_index_handles_history_appearing_later(tmp_path: Path) -> None:
    (tmp_path / "memory").mkdir(exist_ok=True)  # memory dir exists, no history yet
    result = await _run(tmp_path, "phi protocol")
    assert result.data["history_total_matches"] == 0

    _write_history(tmp_path, [_entry(1, "2025-06-16T10:30:00+00:00", content="phi protocol appears")])
    result = await _run(tmp_path, "phi protocol")
    assert result.data["history_total_matches"] == 1
    assert _read_index(tmp_path)["size"] > 0


@pytest.mark.asyncio
async def test_index_disabled_writes_no_index_and_still_searches(tmp_path: Path) -> None:
    _write_history(tmp_path, [_entry(1, "2025-06-16T10:30:00+00:00", content="chi protocol row")])
    before = _snapshot(tmp_path)

    result = await _tool(tmp_path, enable_index=False).execute("chi protocol")

    assert result.data["history_total_matches"] == 1
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "memory" / "search_index.json").exists()


# ---------------------------------------------------------------------------
# v2: offset pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offset_paging_newest_first_with_stable_totals(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(i, f"2025-06-{i:02d}T08:00:00+00:00", content=f"psi protocol record {i}")
        for i in range(1, 8)
    ])

    page0 = await _run(tmp_path, "psi protocol", max_results=3, offset=0)
    page1 = await _run(tmp_path, "psi protocol", max_results=3, offset=3)
    page2 = await _run(tmp_path, "psi protocol", max_results=3, offset=6)

    all_cursors = [m["cursor"] for p in (page0, page1, page2) for m in p.data["history"]]
    assert all_cursors == [7, 6, 5, 4, 3, 2, 1]  # newest-first, no overlap, no gap
    assert all(p.data["history_total_matches"] == 7 for p in (page0, page1, page2))
    assert page0.data["history_offset"] == 0 and page0.data["history_has_more"] is True
    assert page1.data["history_offset"] == 3 and page1.data["history_has_more"] is True
    assert page2.data["history_offset"] == 6 and page2.data["history_has_more"] is False
    assert page2.data["history_truncated"] is False
    assert page0.data["history_truncated"] is True
    # The last page still reports the full total even when short.
    assert len(page2.data["history"]) == 1


@pytest.mark.asyncio
async def test_offset_beyond_end_returns_empty_page(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(1, "2025-06-16T10:30:00+00:00", content="omega protocol"),
    ])
    result = await _run(tmp_path, "omega protocol", max_results=10, offset=5)

    assert result.data["history"] == []
    assert result.data["history_total_matches"] == 1
    assert result.data["history_has_more"] is False


@pytest.mark.asyncio
async def test_negative_offset_is_retryable_error(tmp_path: Path) -> None:
    _write_history(tmp_path, [_entry(1, "2025-06-16T10:30:00+00:00", content="omega protocol")])
    result = await _run(tmp_path, "omega protocol", offset=-1)
    assert result.status == "retryable_error"
    assert "offset" in str(result)
    # The index must not be built for a rejected call.
    assert not (tmp_path / "memory" / "search_index.json").exists()


# ---------------------------------------------------------------------------
# v2: includeSystemFiles config knob
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_system_files_toggle(tmp_path: Path) -> None:
    _write_history(tmp_path, [])
    mem = tmp_path / "memory"
    (mem / "homelab.md").write_text("# Homelab\n- alpha system toggle note\n", encoding="utf-8")
    (mem / "MEMORY.md").write_text(
        "---\ntitle: Memory\n---\n\nMEMORY.md mentions alpha system toggle\n", encoding="utf-8"
    )
    (mem / "system").mkdir()
    (mem / "system" / "now.md").write_text("alpha system toggle in system/now.md\n", encoding="utf-8")

    default = await _run(tmp_path, "alpha system toggle")
    default_paths = [f["path"] for f in default.data["memory_files"]]
    assert default_paths == ["memory/homelab.md"]

    enabled = await _tool(tmp_path, include_system_files=True).execute("alpha system toggle")
    paths = {f["path"] for f in enabled.data["memory_files"]}
    assert paths == {
        "memory/homelab.md",
        "memory/MEMORY.md",
        "memory/system/now.md",
    }
    # Same return shape: per-file (line, text) match entries, frontmatter skipped.
    sys_file = next(f for f in enabled.data["memory_files"] if f["path"] == "memory/system/now.md")
    assert sys_file["matches"][0]["line"] == 1
    assert "system/now.md" in sys_file["matches"][0]["text"]
    mem_file = next(f for f in enabled.data["memory_files"] if f["path"] == "memory/MEMORY.md")
    assert mem_file["matches"][0]["line"] == 5  # frontmatter (3 lines) is not searched
    assert "MEMORY.md mentions" in mem_file["matches"][0]["text"]


def test_tools_config_memory_search_knob_camel_case_alias(tmp_path: Path) -> None:
    """tools.memorySearch.includeSystemFiles / enableIndex load via camelCase."""
    from nanobot.config.loader import load_config

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "tools": {
            "memorySearch": {
                "includeSystemFiles": True,
                "enableIndex": False,
            },
        },
    }), encoding="utf-8")
    cfg = load_config(config_path)

    assert cfg.tools.memory_search.include_system_files is True
    assert cfg.tools.memory_search.enable_index is False

    # snake_case spelling works too (populate_by_name on the tool DTO).
    config_path.write_text(json.dumps({
        "tools": {"memorySearch": {"include_system_files": True}},
    }), encoding="utf-8")
    cfg2 = load_config(config_path)
    assert cfg2.tools.memory_search.include_system_files is True
    # And the defaults are index-on / system-files-off.
    cfg3 = load_config(tmp_path / "missing.json")
    assert cfg3.tools.memory_search.enable_index is True
    assert cfg3.tools.memory_search.include_system_files is False


def test_memory_search_tool_create_reads_config(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from nanobot.config.loader import load_config

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "tools": {"memorySearch": {"includeSystemFiles": True, "enableIndex": False}},
    }), encoding="utf-8")
    cfg = load_config(config_path)
    ctx = SimpleNamespace(config=cfg.tools, workspace=str(tmp_path))
    tool = MemorySearchTool.create(ctx)
    assert isinstance(tool, MemorySearchTool)
    assert tool._include_system_files is True
    assert tool._enable_index is False
    assert MemorySearchTool.config_key == "memory_search"
