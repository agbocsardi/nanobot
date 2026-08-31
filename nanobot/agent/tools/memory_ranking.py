"""Local relevance scoring for memory_search history results (v3).

Retrieval-only: pure computation over already-matched results; never writes
to memory and never promotes facts into memory.

The scorer is dependency-free and deterministic. memory_search matches
literally, so every matched text already contains the whole query string and
the overlap term collapses to a constant 1.0 (degenerate by design). The score
is ``1.0`` plus a capped term-frequency nudge so texts that repeat query terms
rank above single mentions; ties fall through to the caller's
timestamp-descending sort.
"""

from __future__ import annotations

import re

_WORD_RE = re.compile(r"[a-z0-9]+")
_MAX_TERM_COUNT = 3  # cap per-query-term multiplicity
_TF_WEIGHT = 0.1  # term-frequency nuance; stays below any meaningful gap


def tokenize(text: str) -> list[str]:
    """Simple case-insensitive tokenization: runs of letters/digits."""
    return _WORD_RE.findall(text.lower())


def overlap_score(query: str, match_text: str) -> float:
    """``1.0 + tf_boost`` for a literal-matched text.

    The query is guaranteed present in the matched text, so set-overlap is
    degenerate (always 1.0); only query-term repetition separates results.
    Single mentions score ``1.0``; repeated mentions score up to
    ``1.0 + _TF_WEIGHT``. An empty query also scores ``1.0``.
    """
    query_terms = tokenize(query)
    if not query_terms:
        return 1.0
    query_set = set(query_terms)
    counts: dict[str, int] = {}
    for term in tokenize(match_text):
        if term in query_set:
            counts[term] = min(_MAX_TERM_COUNT, counts.get(term, 0) + 1)
    if not counts:
        return 1.0
    avg_count = sum(counts.values()) / len(counts)
    tf_boost = _TF_WEIGHT * min(1.0, (avg_count - 1.0) / 2.0)
    return 1.0 + tf_boost


class LocalOverlapScorer:
    """Deterministic local scorer: ``1.0 + tf_boost``.

    Pure Python, no dependencies, no network. ``timestamp`` is accepted for a
    uniform scorer interface but ignored; the caller break ties by timestamp
    descending.
    """

    name = "local-overlap"

    def score(self, query: str, match_text: str, timestamp: str | None = None) -> float:
        return round(overlap_score(query, match_text), 6)
