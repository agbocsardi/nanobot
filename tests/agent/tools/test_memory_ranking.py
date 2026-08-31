"""v3 tests: relevance ranking for memory_search (memory_search issue #1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.memory_ranking import LocalOverlapScorer, overlap_score, tokenize
from nanobot.agent.tools.memory_search import MemorySearchTool


def _write_history(workspace: Path, rows: list[dict]) -> Path:
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


async def _run(
    workspace: Path,
    query: str,
    *,
    tool_kwargs: dict | None = None,
    **kwargs,
) -> ToolResult:
    tool = _tool(workspace, **(tool_kwargs or {}))
    return await tool.execute(query, **kwargs)


# ---------------------------------------------------------------------------
# unit: LocalOverlapScorer (deterministic, dependency-free)
# ---------------------------------------------------------------------------


def test_tokenize_is_case_insensitive_and_simple() -> None:
    assert tokenize("Rewrite Protocol!") == ["rewrite", "protocol"]
    assert tokenize("na\u00efve cr\u00e8me") == ["na", "ve", "cr", "me"]
    assert tokenize("!!!") == []


def test_overlap_score_is_one_plus_tf_nudge() -> None:
    """Literal matching guarantees the query is present: score starts at 1.0.

    Only query-term repetition raises it; a missing-token match still scores
    exactly 1.0 (never 0), and an unparsable query also scores 1.0.
    """
    assert overlap_score("rewrite protocol", "the rewrite protocol module") == 1.0
    assert overlap_score("rewrite protocol", "just the protocol part") == 1.0
    assert overlap_score("rewrite protocol", "no overlap here") == 1.0
    assert overlap_score("!!!", "anything at all") == 1.0
    assert overlap_score("", "anything") == 1.0


def test_overlap_score_term_frequency_is_mild_and_capped() -> None:
    """Repeated query terms nudge the score up, capped at _MAX_TERM_COUNT."""
    single = overlap_score("rewrite protocol", "rewrite protocol module")
    repeated = overlap_score(
        "rewrite protocol", "rewrite protocol rewrite protocol rewrite protocol module"
    )
    assert single == 1.0
    assert repeated > single  # repetition adds a small boost
    assert repeated - single < 0.11  # within _TF_WEIGHT (float tolerance)
    # Capped: 4x and 8x repeats score the same as 3x (cap at 3 per term).
    cap3 = overlap_score("rewrite protocol", "rewrite protocol " * 3)
    cap8 = overlap_score("rewrite protocol", "rewrite protocol " * 8)
    assert cap8 == cap3


def test_local_overlap_scope_is_retrieval_only() -> None:
    """Scoring is pure computation: no writes, no state mutation."""
    scorer = LocalOverlapScorer()
    before = set(scorer.__dict__)
    scorer.score("query terms", "some matched text", "2025-06-01")
    scorer.score("query", "other")
    assert set(scorer.__dict__) == before


# ---------------------------------------------------------------------------
# tool-level: ranking="local" ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_ranking_overlap_dominates_recency(tmp_path: Path) -> None:
    """Repeated-mention older entry outranks single-mention newer one; recency
    ranking keeps newest-first."""
    _write_history(tmp_path, [
        _entry(1, "2025-12-01T08:00:00+00:00",
               content="rewrite protocol module, one mention"),
        _entry(2, "2025-06-01T08:00:00+00:00",
               content="rewrite protocol rewrite protocol rewrite protocol, older"),
    ])

    local = await _run(tmp_path, "rewrite protocol", tool_kwargs={"ranking": "local"})
    recency = await _run(tmp_path, "rewrite protocol")

    assert [m["cursor"] for m in local.data["history"]] == [2, 1]  # overlap beats recency
    assert [m["cursor"] for m in recency.data["history"]] == [1, 2]  # recency unchanged
    scores = {m["cursor"]: m["score"] for m in local.data["history"]}
    assert scores[2] > scores[1]
    assert local.data["ranking"] == "local"
    assert recency.data["ranking"] == "recency"
    assert all(m["score"] is None for m in recency.data["history"])


@pytest.mark.asyncio
async def test_local_ranking_same_score_newer_first(tmp_path: Path) -> None:
    """Equal scores fall through to timestamp descending (tiebreak in caller)."""
    _write_history(tmp_path, [
        _entry(1, "2025-06-01T08:00:00+00:00", content="rewrite protocol module"),
        _entry(2, "2025-12-01T08:00:00+00:00", content="rewrite protocol module"),
    ])

    result = await _run(tmp_path, "rewrite protocol", tool_kwargs={"ranking": "local"})

    assert [m["cursor"] for m in result.data["history"]] == [2, 1]
    assert result.data["history"][0]["score"] == result.data["history"][1]["score"] == 1.0


@pytest.mark.asyncio
async def test_local_ranking_deterministic_and_index_independent(tmp_path: Path) -> None:
    rows = [
        _entry(i, f"2025-06-{i:02d}T08:00:00+00:00", content=f"zeta protocol topic record {i}")
        for i in range(1, 8)
    ]
    rows[0]["content"] = "zeta protocol"
    rows[6]["content"] = "zeta protocol alpha beta gamma"
    _write_history(tmp_path, rows)

    first = await _run(tmp_path, "zeta protocol", tool_kwargs={"ranking": "local"})
    second = await _run(tmp_path, "zeta protocol", tool_kwargs={"ranking": "local"})
    no_index = await _tool(tmp_path, ranking="local", enable_index=False).execute("zeta protocol")

    def seq(res) -> list[tuple]:
        return [(m["cursor"], m["score"]) for m in res.data["history"]]

    assert seq(second) == seq(first)  # deterministic across repeated runs
    assert seq(no_index) == seq(first)  # ranking applies post-search, index-independent
    assert all(isinstance(m["score"], float) for m in first.data["history"])


@pytest.mark.asyncio
async def test_local_ranking_respects_filters_and_paging(tmp_path: Path) -> None:
    rows = [
        _entry(i, f"2025-06-{i:02d}T08:00:00+00:00", content=f"iota protocol record {i}")
        for i in range(1, 8)
    ]
    _write_history(tmp_path, rows)

    page0 = await _run(tmp_path, "iota protocol", tool_kwargs={"ranking": "local"}, max_results=3, offset=0)
    page2 = await _run(tmp_path, "iota protocol", tool_kwargs={"ranking": "local"}, max_results=3, offset=6)

    assert [m["cursor"] for m in page0.data["history"]] == [7, 6, 5]  # ties -> timestamp desc
    assert page0.data["history_total_matches"] == 7
    assert len(page2.data["history"]) == 1 and page2.data["history_total_matches"] == 7
    assert page0.data["history_offset"] == 0 and page0.data["history_has_more"] is True
    assert page2.data["history_offset"] == 6 and page2.data["history_has_more"] is False


@pytest.mark.asyncio
async def test_local_ranking_text_shows_scores_recency_text_does_not(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(1, "2025-06-01T08:00:00+00:00", content="lambda protocol module"),
    ])

    local = await _run(tmp_path, "lambda protocol", tool_kwargs={"ranking": "local"})
    recency = await _run(tmp_path, "lambda protocol")

    assert "score=" in str(local) and "score=1.000" in str(local)
    assert "score=" not in str(recency)
    assert recency.data["ranking"] == "recency"


# ---------------------------------------------------------------------------
# config: camelCase alias, defaults, fail-fast validation
# ---------------------------------------------------------------------------


def test_config_ranking_camel_case_alias_and_defaults(tmp_path: Path) -> None:
    from nanobot.config.loader import load_config

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "tools": {"memorySearch": {"ranking": "local"}},
    }), encoding="utf-8")
    cfg = load_config(config_path)
    assert cfg.tools.memory_search.ranking == "local"

    config_path.write_text(json.dumps({
        "tools": {"memorySearch": {"ranking": "recency"}},
    }), encoding="utf-8")
    cfg2 = load_config(config_path)
    assert cfg2.tools.memory_search.ranking == "recency"

    cfg3 = load_config(tmp_path / "missing.json")
    assert cfg3.tools.memory_search.ranking == "recency"


def test_invalid_ranking_value_is_rejected(tmp_path: Path) -> None:
    from nanobot.config.loader import load_config

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "tools": {"memorySearch": {"ranking": "semantic"}},
    }), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(config_path)
    with pytest.raises(ValueError):
        MemorySearchTool(workspace=tmp_path, ranking="semantic")


# ---------------------------------------------------------------------------
# regression: default recency output is byte-identical to v2
# ---------------------------------------------------------------------------


GOLDEN_CORPUS = [
    {"cursor": 1, "timestamp": "2025-06-10T08:00:00+00:00", "session_key": "telegram:1",
     "content": "older record about the rewrite protocol module"},
    {"cursor": 2, "timestamp": "2025-06-20T08:00:00+00:00",
     "summary": "newer entry: user asked about the rewrite protocol parser",
     "messages": [
         {"timestamp": "2025-06-20T08:05:00+00:00", "role": "user",
          "content": "how does the rewrite protocol parser work?"},
     ]},
    {"cursor": 3, "timestamp": "2025-06-15T10:00:00+00:00", "session_key": "discord:7",
     "content": "unrelated topic"},
]

GOLDEN_V2_TEXT = (
    'query: "rewrite protocol" \u2014 2 history match(es), 1 memory file(s) with matches\n'
    "\n"
    "history:\n"
    "[1] cursor=2 | 2025-06-20T08:00:00+00:00 | summary\n"
    "    newer entry: user asked about the rewrite protocol parser\n"
    "[2] cursor=1 | 2025-06-10T08:00:00+00:00 | session=telegram:1 | content\n"
    "    older record about the rewrite protocol module\n"
    "\n"
    "memory files:\n"
    "memory/notes.md:\n"
    "  1: the rewrite protocol is mentioned in topic notes too"
)


@pytest.mark.asyncio
async def test_recency_default_output_byte_identical_to_v2(tmp_path: Path) -> None:
    _write_history(tmp_path, GOLDEN_CORPUS)
    (tmp_path / "memory" / "notes.md").write_text(
        "the rewrite protocol is mentioned in topic notes too\n", encoding="utf-8"
    )

    result = await _run(tmp_path, "rewrite protocol")

    assert str(result) == GOLDEN_V2_TEXT
    assert result.data["ranking"] == "recency"
    assert [m["score"] for m in result.data["history"]] == [None, None]
    assert "score=" not in str(result)
