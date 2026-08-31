"""v3 tests: relevance ranking for memory_search (memory_search issue #1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.memory_ranking import (
    LocalOverlapScorer,
    ProviderEmbeddingScorer,
    overlap_score,
    resolve_scorer,
    tokenize,
)
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
    """Run with tool-construction kwargs (e.g. ranking) separate from execute kwargs."""
    tool = _tool(workspace, **(tool_kwargs or {}))
    return await tool.execute(query, **kwargs)


# ---------------------------------------------------------------------------
# unit: LocalOverlapScorer (deterministic, dependency-free)
# ---------------------------------------------------------------------------


def test_tokenize_is_case_insensitive_and_simple() -> None:
    assert tokenize("Rewrite Protocol!") == ["rewrite", "protocol"]
    # non-ASCII letters are not [a-z0-9] tokens; they split terms apart
    assert tokenize("na\u00efve cr\u00e8me") == ["na", "ve", "cr", "me"]
    assert tokenize("!!!") == []


def test_overlap_score_exact_values() -> None:
    assert overlap_score("rewrite protocol", "the rewrite protocol module") == 1.0
    assert overlap_score("rewrite protocol", "just the protocol part") == 0.5
    assert overlap_score("rewrite protocol", "no overlap here") == 0.0
    # Unparsable query (no tokens) is treated as a full overlap: the literal
    # search already matched the text, so the scorer must not reject it.
    assert overlap_score("!!!", "anything at all") == 1.0


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
    assert overlap_score("rewrite protocol", "") == 0.0


def test_local_overlap_scorer_recency_boost_is_deterministic_and_mild() -> None:
    scorer = LocalOverlapScorer()
    text = "rewrite protocol module"
    old = scorer.score("rewrite protocol", text, "2025-06-01")
    new = scorer.score("rewrite protocol", text, "2025-12-01")
    assert old == scorer.score("rewrite protocol", text, "2025-06-01")  # repeatable
    assert new > old  # newer date gets the (mild) boost
    assert new <= 1.0 + 0.25  # bounded: 1.0 overlap + max weight
    assert scorer.score("rewrite protocol", text, None) < new  # missing ts = no boost


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
    """Control query: term-overlap order differs from recency order -> overlap wins.

    Literal matching guarantees every match contains the query string, so the
    local scorer separates matches by term multiplicity (set/term overlap):
    the repeated-mention older entry outranks the single-mention newer one,
    while plain recency ranking keeps newest-first.
    """
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
    assert isinstance(scores[2], float) and isinstance(scores[1], float)
    assert scores[2] > scores[1]
    assert local.data["ranking"] == "local"
    assert recency.data["ranking"] == "recency"
    assert all(m["score"] is None for m in recency.data["history"])


@pytest.mark.asyncio
async def test_local_ranking_same_overlap_newer_first(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        _entry(1, "2025-06-01T08:00:00+00:00", content="rewrite protocol module"),
        _entry(2, "2025-12-01T08:00:00+00:00", content="rewrite protocol module"),
    ])

    result = await _run(tmp_path, "rewrite protocol", tool_kwargs={"ranking": "local"})

    assert [m["cursor"] for m in result.data["history"]] == [2, 1]
    assert result.data["history"][0]["score"] > result.data["history"][1]["score"]


@pytest.mark.asyncio
async def test_local_ranking_deterministic_and_index_independent(tmp_path: Path) -> None:
    rows = [
        _entry(i, f"2025-06-{i:02d}T08:00:00+00:00", content=f"zeta protocol topic record {i}")
        for i in range(1, 8)
    ]
    rows[0]["content"] = "zeta protocol"  # shorter text, same term overlap
    rows[6]["content"] = "zeta protocol alpha beta gamma"  # single mention too
    _write_history(tmp_path, rows)

    first = await _run(tmp_path, "zeta protocol", tool_kwargs={"ranking": "local"})
    second = await _run(tmp_path, "zeta protocol", tool_kwargs={"ranking": "local"})
    no_index = await _tool(tmp_path, ranking="local", enable_index=False).execute(
        "zeta protocol"
    )

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

    filtered = await _run(
        tmp_path,
        "iota protocol",
        ranking="local",
        date_from="2025-06-01",
        date_to="2025-06-03",
    )
    assert {m["cursor"] for m in filtered.data["history"]} == {1, 2, 3}


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
# provider boundary: stub raises; injection is used end-to-end
# ---------------------------------------------------------------------------


def test_provider_stub_raises_without_injection() -> None:
    with pytest.raises(NotImplementedError):
        ProviderEmbeddingScorer()
    stub = ProviderEmbeddingScorer.__new__(ProviderEmbeddingScorer)  # bypass __init__
    with pytest.raises(NotImplementedError):
        stub.score("q", "text")
    with pytest.raises(NotImplementedError):
        resolve_scorer("nanobot.agent.tools.memory_ranking.ProviderEmbeddingScorer")
    with pytest.raises(NotImplementedError):
        MemorySearchTool(
            workspace=Path("."),
            ranking="local",
            embedding_scorer="nanobot.agent.tools.memory_ranking.ProviderEmbeddingScorer",
        )


def test_resolve_scorer_validation() -> None:
    with pytest.raises(ValueError):
        resolve_scorer("NotADottedPath")
    with pytest.raises(ValueError):
        resolve_scorer("no_such_module_xyz.Dummy")
    with pytest.raises(ValueError):
        resolve_scorer("nanobot.agent.tools.memory_ranking.tokenize")  # a function
    assert isinstance(resolve_scorer(None), LocalOverlapScorer)
    assert isinstance(resolve_scorer(""), LocalOverlapScorer)


@pytest.mark.asyncio
async def test_injected_dummy_scorer_used_end_to_end(tmp_path: Path) -> None:
    """A concrete scorer injected via config path is used for ranking."""
    _write_history(tmp_path, [
        _entry(1, "2025-06-10T08:00:00+00:00", content="alpha protocol older"),
        _entry(2, "2025-06-20T08:00:00+00:00", content="alpha protocol newer"),
    ])
    # Make a standalone importable module for the fully-qualified path.
    (tmp_path / "dummy_scorer.py").write_text(
        "class DummyScorer:\n"
        "    name = 'dummy'\n"
        "    def score(self, query, match_text, timestamp=None):\n"
        "        return 42.0\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        tool = MemorySearchTool(
            workspace=tmp_path,
            ranking="local",
            embedding_scorer="dummy_scorer.DummyScorer",
        )
        result = await tool.execute("alpha protocol")
    finally:
        sys.path.remove(str(tmp_path))

    assert tool._scorer.name == "dummy"
    assert [m["score"] for m in result.data["history"]] == [42.0, 42.0]
    assert "score=42.000" in str(result)


@pytest.mark.asyncio
async def test_config_plumbs_ranking_and_scorer_into_tool(tmp_path: Path) -> None:
    """MemorySearchTool.create passes ranking + embeddingScorer through."""
    from nanobot.config.loader import load_config

    (tmp_path / "dummy_scorer.py").write_text(
        "class DummyScorer:\n"
        "    name = 'dummy'\n"
        "    def score(self, query, match_text, timestamp=None):\n"
        "        return 1.5\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({
            "tools": {"memorySearch": {
                "ranking": "local",
                "embeddingScorer": "dummy_scorer.DummyScorer",
            }},
        }), encoding="utf-8")
        cfg = load_config(config_path)
        assert cfg.tools.memory_search.ranking == "local"
        assert cfg.tools.memory_search.embedding_scorer == "dummy_scorer.DummyScorer"

        ctx = SimpleNamespace(config=cfg.tools, workspace=str(tmp_path))
        tool = MemorySearchTool.create(ctx)
        assert tool._ranking == "local"
        assert tool._scorer.name == "dummy"
    finally:
        sys.path.remove(str(tmp_path))


# ---------------------------------------------------------------------------
# config: camelCase aliases + validation
# ---------------------------------------------------------------------------


def test_config_ranking_and_scorer_camel_case_alias_and_defaults(tmp_path: Path) -> None:
    from nanobot.config.loader import load_config

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "tools": {"memorySearch": {"ranking": "local", "embeddingScorer": "pkg.mod.Cls"}},
    }), encoding="utf-8")
    cfg = load_config(config_path)
    assert cfg.tools.memory_search.ranking == "local"
    assert cfg.tools.memory_search.embedding_scorer == "pkg.mod.Cls"

    # snake_case spelling works too.
    config_path.write_text(json.dumps({
        "tools": {"memorySearch": {"ranking": "recency"}},
    }), encoding="utf-8")
    cfg2 = load_config(config_path)
    assert cfg2.tools.memory_search.ranking == "recency"

    # Defaults: recency, no injected scorer.
    cfg3 = load_config(tmp_path / "missing.json")
    assert cfg3.tools.memory_search.ranking == "recency"
    assert cfg3.tools.memory_search.embedding_scorer is None


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

# Captured verbatim from `git show main:nanobot/agent/tools/memory_search.py`
# (v2 semantics) running the fixed corpus above.
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

