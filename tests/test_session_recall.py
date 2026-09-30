"""Session-start recall ranking — regression cover for issues #28 and #29.

``recall_session_memories`` used to rebuild its final candidate list by
re-encoding the always-load set as vector hits at a synthetic distance of 0.0
and forwarding only the FTS half of the semantic pool. Between them those two
moves made the semantic signal binary — 1.0 for always-load, 0.0 for
everything else — so the ``alpha`` weight behaved as an always-load flag and
paraphrase-adjacent recall did not work at session start (#28). The always-load
set was itself unbounded, which tied most of a mature corpus at that maximal
score (#29).

These tests pin the repaired contract:

- real vector similarity reaches the scorer, including for candidates that
  **only** vector search found;
- always-load membership is a flag, not a similarity claim;
- the always-load pool stays bounded as the corpus grows.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from claude_memory.db.queries import MemoryRecord, insert_memory
from claude_memory.retrieval.scorer import ScoringWeights
from claude_memory.retrieval.search import (
    _CANDIDATE_POOL_MULTIPLIER,
    SearchResult,
    recall_session_memories,
)

from tests.conftest import MockEncoder


PROJECT: str = "/projects/example"

# Production weights (see claude_memory.config.MemorySettings). The dataclass
# default leaves kappa at 0.0; recall in the field runs with the keyword boost
# live, and these tests should reflect that.
PRODUCTION_WEIGHTS = ScoringWeights(
    alpha=0.45, beta=0.20, gamma=0.10, delta=0.25, kappa=0.30,
)

# Deliberately shares no token with any memory content below, so the lexical
# signal is zero for every candidate and the semantic term is isolated.
DISJOINT_CONTEXT: str = "surfacing prioritised recollections"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert(
    conn: sqlite3.Connection,
    encoder: MockEncoder,
    id: str,
    content: str,
    *,
    importance: float = 5.0,
    tier: str = "hot",
    access_count: int = 5,
    project_dir: str | None = None,
) -> None:
    now: str = datetime.now(timezone.utc).isoformat()
    record = MemoryRecord(
        id=id,
        content=content,
        summary=None,
        type="lesson",
        tags=[],
        created_at=now,
        updated_at=now,
        last_accessed=now,
        access_count=access_count,
        importance=importance,
        tier=tier,
        project_dir=project_dir,
        source_session=None,
        supersedes=None,
        consolidated_from=[],
        metadata={},
    )
    insert_memory(conn, record, encoder.encode(content))
    # Commit so the next scope-aware read can register its SQL helper: an open
    # write transaction plus a cached SELECT makes SQLite refuse
    # create_function (issue #31).
    conn.commit()


def _recall(
    conn: sqlite3.Connection,
    encoder: MockEncoder,
    *,
    vec: list[tuple[str, float]],
    fts: list[tuple[str, float]],
    context: str = DISJOINT_CONTEXT,
    top_k: int = 15,
) -> list[SearchResult]:
    """Run recall with the two search backends pinned to fixed results.

    The conftest encoder hashes text, so semantic neighbourhood cannot be
    expressed through content alone — the vector backend is stubbed instead,
    exactly as the project-scope suite does.
    """
    with (
        patch("claude_memory.retrieval.search.search_vec", return_value=vec),
        patch("claude_memory.retrieval.search.search_fts", return_value=fts),
    ):
        return recall_session_memories(
            conn,
            encoder,
            project_dir=PROJECT,
            initial_context=context,
            weights=PRODUCTION_WEIGHTS,
            top_k=top_k,
        )


# ---------------------------------------------------------------------------
# Issue #28 — the semantic pool must survive the final merge
# ---------------------------------------------------------------------------


class TestSemanticSignalReachesScoring:
    def test_vector_only_candidate_is_not_discarded(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """A candidate found by vector search alone must reach the results.

        It has ``fts_rank is None`` and is not always-load, which is precisely
        the combination the old final merge filtered away.
        """
        _insert(
            db_conn, mock_encoder, "vec-only", "Nearest neighbour in embedding space",
        )

        results = _recall(db_conn, mock_encoder, vec=[("vec-only", 0.30)], fts=[])

        assert [r.memory.id for r in results] == ["vec-only"]
        assert results[0].semantic_score == pytest.approx(0.70)

    def test_semantically_related_lexically_disjoint_memory_surfaces(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """The headline case from issue #28.

        A memory that shares no keyword with the session context, sits below
        the always-load importance threshold, and is reachable only through
        vector similarity must not merely appear — it must outrank the generic
        high-importance memories that always-load contributes. Under the old
        merge it was dropped from the candidate list entirely.
        """
        _insert(
            db_conn,
            mock_encoder,
            "paraphrase-target",
            "Ranking weighs cosine proximity between stored vectors.",
            importance=5.0,
            project_dir=PROJECT,
        )
        for i in range(3):
            _insert(
                db_conn,
                mock_encoder,
                f"generic-{i}",
                f"Unrelated operational note number {i}.",
                importance=9.0,
            )

        results = _recall(
            db_conn, mock_encoder, vec=[("paraphrase-target", 0.20)], fts=[],
        )
        ids = [r.memory.id for r in results]

        assert ids[0] == "paraphrase-target"
        assert results[0].semantic_score == pytest.approx(0.80)
        assert results[0].lexical_score == pytest.approx(0.0)
        # The always-load memories are still recalled — just outranked.
        assert {"generic-0", "generic-1", "generic-2"}.issubset(set(ids))

    def test_always_load_does_not_receive_synthetic_similarity(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """Always-load membership is a flag, not a claim about relevance.

        The old merge injected the set at distance 0.0, which
        ``_semantic_similarity_from_candidate`` turns into similarity 1.0 —
        the full ``alpha`` weight for every member regardless of the query.
        """
        _insert(
            db_conn,
            mock_encoder,
            "always",
            "Operational note that the query never mentions.",
            importance=9.0,
        )

        results = _recall(db_conn, mock_encoder, vec=[], fts=[])

        assert [r.memory.id for r in results] == ["always"]
        assert results[0].semantic_score == pytest.approx(0.0)

    def test_always_load_member_keeps_its_real_similarity_when_it_has_one(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """Being always-load must not erase a genuine vector hit either."""
        _insert(
            db_conn,
            mock_encoder,
            "always",
            "Operational note the query happens to be near.",
            importance=9.0,
        )

        results = _recall(db_conn, mock_encoder, vec=[("always", 0.40)], fts=[])

        assert results[0].semantic_score == pytest.approx(0.60)

    def test_always_load_still_bypasses_tier_thresholds(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """Losing the synthetic 1.0 must not lock cold memories out.

        ``rerank`` requires 0.90 similarity from a cold-tier candidate but
        exempts always-load members. That exemption is what carries a cold
        priority memory into the window now that its similarity is honest.
        """
        _insert(
            db_conn,
            mock_encoder,
            "cold-priority",
            "Long-dormant instruction that still matters.",
            importance=9.0,
            tier="cold",
        )

        results = _recall(db_conn, mock_encoder, vec=[], fts=[])

        assert [r.memory.id for r in results] == ["cold-priority"]
        assert results[0].semantic_score == pytest.approx(0.0)

    def test_empty_context_still_returns_always_load_only(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """No context means no semantic pool — always-load stands alone."""
        _insert(db_conn, mock_encoder, "always", "Priority note.", importance=9.0)
        _insert(db_conn, mock_encoder, "ordinary", "Ordinary note.", importance=5.0)

        results = _recall(db_conn, mock_encoder, vec=[], fts=[], context="")

        assert [r.memory.id for r in results] == ["always"]


# ---------------------------------------------------------------------------
# Issue #29 — the always-load pool must stay bounded
# ---------------------------------------------------------------------------


class TestAlwaysLoadPoolIsBounded:
    def test_candidate_pool_does_not_grow_with_the_corpus(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """Every seeded memory qualifies as always-load, so an unbounded pool
        would hand ``rerank`` 60, then 120, then 180 candidates."""
        top_k: int = 10
        observed: list[int] = []

        for batch in range(3):
            for i in range(60):
                _insert(
                    db_conn,
                    mock_encoder,
                    f"b{batch}-{i:03d}",
                    f"High-importance note {batch}-{i}.",
                    importance=8.0,
                )
            with patch(
                "claude_memory.retrieval.search.rerank", return_value=[],
            ) as spy:
                _recall(db_conn, mock_encoder, vec=[], fts=[], top_k=top_k)
            observed.append(len(spy.call_args.args[0]))

        assert observed == [top_k * _CANDIDATE_POOL_MULTIPLIER] * 3

    def test_project_memories_are_not_crowded_out_by_globals(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """Bounding must not sacrifice the project context the caller asked for.

        A small project against a large global pool is the shape that makes a
        naive ``LIMIT`` regress: ordered by importance alone the globals fill
        the bound, and a project never loads its own memories.
        """
        for i in range(60):
            _insert(
                db_conn,
                mock_encoder,
                f"global-{i:03d}",
                f"Global note {i}.",
                importance=9.0,
            )
        for i in range(2):
            _insert(
                db_conn,
                mock_encoder,
                f"project-{i}",
                f"Project note {i}.",
                importance=7.5,
                project_dir=PROJECT,
            )

        with patch("claude_memory.retrieval.search.rerank", return_value=[]) as spy:
            _recall(db_conn, mock_encoder, vec=[], fts=[], top_k=5)
        pooled = [c.memory_id for c in spy.call_args.args[0]]

        assert len(pooled) == 5 * _CANDIDATE_POOL_MULTIPLIER
        assert {"project-0", "project-1"}.issubset(set(pooled))
