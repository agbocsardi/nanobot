"""Pluggable relevance ranking for memory_search history results (v3).

v3 additions (memory_search GitHub issue #1 continuation):

- Ranking modes behind ``tools.memorySearch.ranking``:
  * ``"recency"`` (default) — the v1/v2 newest-first order, unchanged.
  * ``"local"`` — a deterministic local relevance scorer with NO extra
    dependencies: token overlap (query tokens vs match text, simple
    case-insensitive tokenization, set/term overlap) combined with a mild
    recency boost. Pure Python; no embeddings, no network.
- ``EmbeddingScorer`` protocol: the explicit provider boundary.  The
  built-in implementation is :class:`LocalOverlapScorer`.  Provider-backed
  scoring is opt-in and unwired: :class:`ProviderEmbeddingScorer` is a stub
  that raises ``NotImplementedError`` at construction unless a concrete
  implementation is injected via config
  (``tools.memorySearch.embeddingScorer`` = ``"package.module.ClassName"``).

Retrieval-only: scoring is pure computation over already-matched results. It
never writes to memory and never promotes facts into memory.

Determinism: the recency decay is anchored at a module-level constant
(``_ANCHOR_ISO``), so scores for a fixed corpus are stable across runs and
platforms.  The boost is deliberately mild (``_RECENCY_WEIGHT``) so overlap
dominates ordering; equal scores fall back to timestamp-descending order in
the caller.
"""

from __future__ import annotations

import importlib
import math
import re
from typing import Protocol, runtime_checkable

_WORD_RE = re.compile(r"[a-z0-9]+")

# Fixed "present" anchor for the deterministic recency decay.  A constant (not
# wall-clock time) keeps the scorer reproducible across runs.
_ANCHOR_ISO = "2026-01-01"
_RECENCY_WEIGHT = 0.25  # mild: overlap dominates; boost only breaks ties
_RECENCY_HALF_LIFE_DAYS = 30.0  # gentle exponential decay
_MAX_TERM_COUNT = 3  # cap per-query-term multiplicity in term-overlap
_TF_WEIGHT = 0.1  # term-frequency nuance; stays well below overlap gaps


def _day_number(ts: str) -> float:
    """Serial day number (approximate) for a timestamp prefix.

    Accepts ``YYYY-MM-DD``, ``YYYY-MM-DD HH:MM`` or ISO
    ``YYYY-MM-DDTHH:MM:SS...`` prefixes.  Unparsable input yields 0.0.  The
    approximation (fixed month length) is plenty for a mild exponential decay.
    """
    if len(ts) < 10:
        return 0.0
    try:
        year = int(ts[0:4])
        month = int(ts[5:7])
        day = int(ts[8:10])
    except (ValueError, IndexError):
        return 0.0
    return year * 365.25 + (month - 1) * 30.44 + day


_ANCHOR_DAYS = _day_number(_ANCHOR_ISO)


def tokenize(text: str) -> list[str]:
    """Simple case-insensitive tokenization: runs of letters/digits."""
    return _WORD_RE.findall(text.lower())


def _recency_boost(timestamp: str | None) -> float:
    """Mild deterministic recency boost in [0, ``_RECENCY_WEIGHT``]."""
    if not timestamp:
        return 0.0
    age_days = max(0.0, _ANCHOR_DAYS - _day_number(timestamp))
    return _RECENCY_WEIGHT * math.exp(-age_days / _RECENCY_HALF_LIFE_DAYS)


def overlap_score(query: str, match_text: str) -> float:
    """Deterministic term-overlap of query vs match text.

    ``coverage`` is the set overlap (``|query_terms ∩ text_terms| /
    |query_terms|``).  Because memory_search matches literally, every matched
    text already contains the whole query string, so coverage alone is
    degenerate (always 1.0 for a real match); a mild term-frequency boost
    (capped per-term multiplicity, ``_TF_WEIGHT`` weighted) is added so texts
    that repeat query terms rank above single mentions.  The boost stays
    smaller than a one-term overlap gap (``1 / |query_terms|``) for typical
    queries.  No query tokens -> full overlap (the literal search matched).
    """
    query_terms = tokenize(query)
    if not query_terms:
        return 1.0
    query_set = set(query_terms)
    counts: dict[str, int] = {}
    for term in tokenize(match_text):
        if term in query_set:
            counts[term] = min(_MAX_TERM_COUNT, counts.get(term, 0) + 1)
    coverage = len(counts) / len(query_set)
    if not counts:
        return 0.0  # nothing matched: no overlap, no term-frequency bonus
    # Multiplicity averaged over the *covered* terms only (partial coverage
    # must not depress it below the single-mention baseline).
    avg_count = sum(counts.values()) / len(counts)
    tf_boost = _TF_WEIGHT * min(1.0, (avg_count - 1.0) / 2.0)
    return coverage + tf_boost


@runtime_checkable
class EmbeddingScorer(Protocol):
    """Interface for scoring a matched result's relevance to a query.

    Implementations are retrieval-only: they compute a score from the query
    and matched text and must not write to memory or mutate state.
    ``timestamp`` is the normalized ``YYYY-MM-DD HH:MM`` (or ``None``) of the
    match, for scorers that want it.
    """

    name: str

    def score(self, query: str, match_text: str, timestamp: str | None = None) -> float: ...


class LocalOverlapScorer:
    """Deterministic local scorer: token overlap + mild recency boost.

    ``score = overlap_score(query, text) + recency_boost(timestamp)`` in
    ``[0, 1 + _TF_WEIGHT + _RECENCY_WEIGHT]``.  Term overlap dominates; the
    boosts only nudge near-ties.  Pure Python, no dependencies, no network.
    """

    name = "local-overlap"

    def score(self, query: str, match_text: str, timestamp: str | None = None) -> float:
        return overlap_score(query, match_text) + _recency_boost(timestamp)


class ProviderEmbeddingScorer:
    """Documented stub for provider-backed embedding scoring.

    Retrieval-only boundary, and deliberately NOT wired to any provider in
    this repository.  Instantiating this class raises ``NotImplementedError``;
    a concrete implementation must be injected via config::

        {"tools": {"memorySearch": {
            "ranking": "local",
            "embeddingScorer": "my_pkg.scorers.MyScorer",
        }}}

    A concrete scorer typically subclasses this class and overrides
    ``__init__`` (without calling ``super().__init__()``) and ``score()`` —
    or simply implements the :class:`EmbeddingScorer` protocol directly.
    """

    name = "provider-embedding"

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "ProviderEmbeddingScorer is a retrieval-only stub. Inject a concrete "
            "scorer via tools.memorySearch.embeddingScorer (fully-qualified "
            "'package.module.ClassName') — no provider is wired in this repo."
        )

    def score(self, query: str, match_text: str, timestamp: str | None = None) -> float:
        raise NotImplementedError(
            "ProviderEmbeddingScorer.score() is unimplemented; inject a concrete "
            "scorer via tools.memorySearch.embeddingScorer."
        )


def resolve_scorer(embedding_scorer_path: str | None) -> EmbeddingScorer:
    """Build the scorer for ranking='local'.

    With no ``embedding_scorer_path`` returns :class:`LocalOverlapScorer`.
    With a path, imports ``package.module.ClassName`` and instantiates it.
    The ProviderEmbeddingScorer stub raises ``NotImplementedError`` here
    (no injection); import/attribute/interface errors raise ``ValueError``.
    """
    if not embedding_scorer_path:
        return LocalOverlapScorer()
    module_name, sep, attr_name = embedding_scorer_path.rpartition(".")
    if not sep or not module_name or not attr_name:
        raise ValueError(
            "tools.memorySearch.embeddingScorer must be a fully-qualified "
            f'"package.module.ClassName" path, got {embedding_scorer_path!r}'
        )
    try:
        module = importlib.import_module(module_name)
        scorer_cls = getattr(module, attr_name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(
            f"cannot resolve tools.memorySearch.embeddingScorer "
            f"{embedding_scorer_path!r}: {exc}"
        ) from exc
    try:
        # NotImplementedError for the stub: no injection.
        scorer = scorer_cls()
    except TypeError as exc:
        raise ValueError(
            f"tools.memorySearch.embeddingScorer {embedding_scorer_path!r} "
            f"cannot be instantiated: {exc}"
        ) from exc
    if not isinstance(scorer, EmbeddingScorer):
        raise ValueError(
            f"tools.memorySearch.embeddingScorer {embedding_scorer_path!r} does not "
            "implement EmbeddingScorer (needs a 'name' attribute and a "
            "'score(query, match_text, timestamp=None)' method)"
        )
    return scorer
