"""Tests for Phase-2 additions: keyword-overlap boost and token-budget truncation.

Covers:

- ``_tokenize`` and ``_STOP_WORDS`` — the query/memory tokeniser used for the
  lexical overlap signal.
- ``_keyword_overlap`` — Jaccard-lite fraction of query tokens present in a
  memory record (content + tags).
- ``compute_combined_score`` with ``lexical_score`` parameter and
  ``ScoringWeights.kappa`` — verifies backward compatibility (kappa=0 is a
  no-op) and additive-boost semantics (kappa=0.3 adds up to +0.3 to score).
- ``rerank(... query=...)`` — end-to-end test that a keyword-matching
  memory outranks a semantically-close-but-keyword-distant competitor.
- ``_estimate_tokens`` and ``_truncate_to_token_budget`` — token-budget
  trimming used by hook-injected recall output.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from claude_memory.db.queries import MemoryRecord
from claude_memory.retrieval.reranker import (
    RetrievalCandidate,
    _STOP_WORDS,
    _keyword_overlap,
    _tokenize,
    rerank,
)
from claude_memory.retrieval.scorer import ScoringWeights, compute_combined_score
from claude_memory.retrieval.search import (
    SearchResult,
    _estimate_tokens,
    _truncate_to_token_budget,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory_record(
    id: str = "m1",
    content: str = "placeholder",
    tags: list[str] | None = None,
    tier: str = "hot",
    importance: float = 5.0,
    access_count: int = 5,
    days_ago: float = 0.0,
) -> MemoryRecord:
    """Create a MemoryRecord for testing (not inserted into any database)."""
    now: datetime = datetime.now(timezone.utc)
    last_accessed: str = (now - timedelta(days=days_ago)).isoformat()
    return MemoryRecord(
        id=id,
        content=content,
        summary=None,
        type="lesson",
        tags=tags or [],
        created_at=(now - timedelta(days=30)).isoformat(),
        updated_at=now.isoformat(),
        last_accessed=last_accessed,
        access_count=access_count,
        importance=importance,
        tier=tier,
        project_dir=None,
        source_session=None,
        supersedes=None,
        consolidated_from=[],
        metadata={},
    )


def _make_search_result(content: str, score: float = 0.5) -> SearchResult:
    return SearchResult(
        memory=_make_memory_record(content=content),
        score=score,
        semantic_score=score,
        recency_score=1.0,
        frequency_score=0.0,
        importance_score=0.5,
        lexical_score=0.0,
    )


# ---------------------------------------------------------------------------
# _tokenize / _STOP_WORDS
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_empty_returns_empty_set(self) -> None:
        assert _tokenize("") == set()

    def test_drops_stopwords(self) -> None:
        result: set[str] = _tokenize("What is the purpose of the login flow?")
        # Wh-words and articles gone.
        assert "what" not in result
        assert "the" not in result
        # Content words kept.
        assert "purpose" in result
        assert "login" in result
        assert "flow" in result

    def test_drops_short_tokens(self) -> None:
        """Tokens shorter than 3 chars are dropped (filters out noise)."""
        result: set[str] = _tokenize("a bb ccc dddd")
        assert "a" not in result
        assert "bb" not in result
        assert "ccc" in result
        assert "dddd" in result

    def test_preserves_underscores(self) -> None:
        """Snake_case identifiers stay as single tokens."""
        result: set[str] = _tokenize("memory_store and memory_recall use snake_case")
        assert "memory_store" in result
        assert "memory_recall" in result
        assert "snake_case" in result

    def test_is_case_insensitive(self) -> None:
        result: set[str] = _tokenize("ForumThread AuthorId SignalR")
        assert "forumthread" in result
        assert "authorid" in result
        assert "signalr" in result

    def test_stop_words_covers_common_question_words(self) -> None:
        for stop in ("what", "when", "how", "which", "was", "the", "of"):
            assert stop in _STOP_WORDS, f"expected {stop!r} in STOP_WORDS"


# ---------------------------------------------------------------------------
# _keyword_overlap
# ---------------------------------------------------------------------------


class TestKeywordOverlap:
    def test_empty_query_returns_zero(self) -> None:
        record = _make_memory_record(content="anything")
        assert _keyword_overlap(set(), record) == 0.0

    def test_all_query_tokens_match(self) -> None:
        record = _make_memory_record(
            content="Forum threads use the authorId field",
        )
        overlap: float = _keyword_overlap(_tokenize("forum authorId"), record)
        assert overlap == 1.0

    def test_partial_match(self) -> None:
        record = _make_memory_record(content="User preferences stored in profile")
        # "user" matches; "authentication" doesn't → 1 / 2 = 0.5.
        overlap: float = _keyword_overlap(_tokenize("user authentication"), record)
        assert overlap == 0.5

    def test_no_match(self) -> None:
        record = _make_memory_record(content="completely unrelated content here")
        assert _keyword_overlap(_tokenize("forum authorId"), record) == 0.0

    def test_includes_tags(self) -> None:
        """Tags contribute to the record's token set alongside content."""
        record = _make_memory_record(
            content="content text", tags=["forum", "signalr"],
        )
        # "signalr" from tags matches 1/2 query tokens.
        assert _keyword_overlap(_tokenize("signalr broadcasts"), record) == 0.5

    def test_handles_json_string_tags(self) -> None:
        """Defensive: MemoryRecord tags can be either list or JSON string."""
        record = _make_memory_record(content="any content")
        record.tags = json.dumps(["forum", "signalr"])  # type: ignore[assignment]
        assert _keyword_overlap(_tokenize("signalr"), record) == 1.0


# ---------------------------------------------------------------------------
# compute_combined_score with lexical_score + kappa
# ---------------------------------------------------------------------------


class TestCombinedScoreLexical:
    def test_kappa_zero_is_backward_compat(self) -> None:
        """With κ=0 (dataclass default), lexical_score has no effect on score."""
        weights = ScoringWeights()  # kappa=0
        base = compute_combined_score(
            semantic_similarity=0.8,
            days_since_access=0.0,
            access_count=10,
            importance=7.0,
            weights=weights,
        )
        boosted = compute_combined_score(
            semantic_similarity=0.8,
            days_since_access=0.0,
            access_count=10,
            importance=7.0,
            weights=weights,
            lexical_score=1.0,
        )
        assert base.score == boosted.score

    def test_kappa_positive_adds_to_score(self) -> None:
        weights = ScoringWeights(kappa=0.30)
        without = compute_combined_score(
            semantic_similarity=0.8,
            days_since_access=0.0,
            access_count=10,
            importance=7.0,
            weights=weights,
            lexical_score=0.0,
        )
        with_ = compute_combined_score(
            semantic_similarity=0.8,
            days_since_access=0.0,
            access_count=10,
            importance=7.0,
            weights=weights,
            lexical_score=1.0,
        )
        assert abs(with_.score - without.score - 0.30) < 1e-9

    def test_lexical_score_clamped_to_valid_range(self) -> None:
        weights = ScoringWeights(kappa=0.30)
        sc = compute_combined_score(
            semantic_similarity=0.5,
            days_since_access=0.0,
            access_count=0,
            importance=5.0,
            weights=weights,
            lexical_score=2.0,  # out-of-range; should clamp to 1.0
        )
        assert sc.lexical_score == 1.0


# ---------------------------------------------------------------------------
# rerank() with query parameter
# ---------------------------------------------------------------------------


class TestRerankWithQuery:
    def test_keyword_matching_memory_outranks_competitor(self) -> None:
        """With κ > 0 and an identical semantic score, the memory whose
        content literally matches the query should rank first."""
        match = _make_memory_record(
            id="match",
            content="The authorId field must be set on forum threads.",
        )
        off_topic = _make_memory_record(
            id="off_topic",
            content="Completely different topic about database schemas.",
        )
        # Equal semantic distance → only lexical boost can differentiate.
        candidates = [
            RetrievalCandidate(memory_id="match", vec_distance=0.3),
            RetrievalCandidate(memory_id="off_topic", vec_distance=0.3),
        ]
        records = {"match": match, "off_topic": off_topic}

        scored = rerank(
            candidates, records,
            weights=ScoringWeights(kappa=0.30),
            query="forum authorId threads",
        )

        assert scored[0].memory_id == "match"
        match_score = next(s for s in scored if s.memory_id == "match")
        off_score = next(s for s in scored if s.memory_id == "off_topic")
        assert match_score.lexical_score > 0.0
        assert off_score.lexical_score == 0.0
        assert match_score.score > off_score.score

    def test_no_query_means_zero_lexical(self) -> None:
        record = _make_memory_record(content="forum threads")
        candidates = [RetrievalCandidate(memory_id="m1", vec_distance=0.5)]
        records = {"m1": record}
        scored = rerank(
            candidates, records, weights=ScoringWeights(kappa=0.30),
        )
        assert scored[0].lexical_score == 0.0

    def test_kappa_zero_ignores_query(self) -> None:
        """If κ=0, passing a query must not meaningfully change the score.

        We use an epsilon rather than strict equality because ``rerank`` reads
        ``datetime.now()`` for the recency signal and the two calls happen
        microseconds apart — a difference that shouldn't fail a semantic test.
        """
        record = _make_memory_record(content="forum threads authorId")
        candidates = [RetrievalCandidate(memory_id="m1", vec_distance=0.5)]
        records = {"m1": record}
        with_query = rerank(
            candidates, records,
            weights=ScoringWeights(kappa=0.0),
            query="forum authorId",
        )[0]
        without_query = rerank(
            candidates, records,
            weights=ScoringWeights(kappa=0.0),
        )[0]
        # Lexical boost is computed but multiplied by κ=0, so the composite
        # scores should match within floating-point noise.
        assert abs(with_query.score - without_query.score) < 1e-6


# ---------------------------------------------------------------------------
# Token budget truncation
# ---------------------------------------------------------------------------


class TestTokenBudget:
    def test_estimate_tokens_empty_is_zero(self) -> None:
        assert _estimate_tokens("") == 0

    def test_estimate_tokens_nonempty_is_positive(self) -> None:
        assert _estimate_tokens("hello world") > 0

    def test_budget_fits_all_results(self) -> None:
        results = [_make_search_result("short"), _make_search_result("also short")]
        out = _truncate_to_token_budget(results, token_budget=10_000)
        assert len(out) == 2

    def test_budget_truncates_at_first_exceed(self) -> None:
        """After the first result that would exceed the budget, stop."""
        large = "x" * 2000  # ~500 tokens by heuristic, enough to fill a small budget
        small = "y"
        results = [
            _make_search_result(large),
            _make_search_result(small),
            _make_search_result(small),
        ]
        out = _truncate_to_token_budget(results, token_budget=50)
        # Top-1 is always kept, but large content alone exceeds budget so
        # no further items fit.
        assert len(out) == 1
        assert out[0].memory.content == large

    def test_top_1_always_included(self) -> None:
        """Top-1 must be returned even if alone it exceeds the budget."""
        huge = "z" * 10_000
        results = [_make_search_result(huge)]
        out = _truncate_to_token_budget(results, token_budget=5)
        assert len(out) == 1

    def test_nonpositive_budget_is_no_op(self) -> None:
        results = [_make_search_result("a"), _make_search_result("b")]
        assert _truncate_to_token_budget(results, token_budget=0) == results
        assert _truncate_to_token_budget(results, token_budget=-1) == results

    def test_empty_input_returns_empty(self) -> None:
        assert _truncate_to_token_budget([], token_budget=100) == []
