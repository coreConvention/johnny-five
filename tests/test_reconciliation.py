"""Tests for Tier A / A3 — contradiction-reconciliation in consolidate (WU-3).

Covers the §6 adversarial hunt list:
* auto-supersede firing without human confirm (detection must NOT mutate);
* hard-delete instead of supersede+archive;
* false-positive clustering (dissimilar / equal-timestamp pairs must not flag);
* lost lineage (supersedes must be set and resolve via memory_why);
* inverted direction (archiving the newer instead of the older).

Plus the retrieval-gap fix WU-3 depends on: an archived (superseded) memory must
stop surfacing in search/recall.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi import HTTPException

from claude_memory.api.routes import (
    ReconcileApplyRequest,
    reconciliation_apply,
    reconciliation_candidates,
)
from claude_memory.db.queries import (
    MemoryRecord,
    get_memory,
    insert_memory,
    list_memories,
    update_memory,
)
from claude_memory.lifecycle.consolidation import (
    ReconciliationError,
    apply_reconciliation,
    find_reconciliation_candidates,
)
from claude_memory.mcp.tools import (
    _memory_why_dict,
    tool_reconciliation_apply,
    tool_reconciliation_candidates,
)
from claude_memory.retrieval.reranker import RetrievalCandidate, rerank
from claude_memory.retrieval.search import search_memories

# Two identical vectors → cosine similarity 1.0 (a reconciliation candidate);
# a near-orthogonal vector stays well below the 0.75 threshold.
_SAME = [0.1] * 384
_DIFF = [1.0] + [0.0] * 383

OLD_TS = "2026-01-01T00:00:00+00:00"
MID_TS = "2026-03-01T00:00:00+00:00"
NEW_TS = "2026-06-01T00:00:00+00:00"


class _NoCloseConn:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


def _insert(
    conn: sqlite3.Connection,
    id: str,
    content: str,
    embedding: list[float],
    created_at: str,
    *,
    tier: str = "hot",
    tags: list[str] | None = None,
    updated_at: str | None = None,
) -> None:
    # created_at drives the reconciliation direction (immutable); updated_at
    # defaults to it but can be overridden to simulate aging churn.
    rec = MemoryRecord(
        id=id, content=content, summary=None, type="lesson", tags=tags or [],
        created_at=created_at, updated_at=updated_at or created_at,
        last_accessed=created_at, access_count=0, importance=5.0, tier=tier,
        project_dir=None, source_session=None, supersedes=None,
        consolidated_from=[], metadata={},
    )
    insert_memory(conn, rec, embedding)


def _seed_contradiction(conn: sqlite3.Connection) -> None:
    """A stale/current near-duplicate pair + one unrelated memory."""
    _insert(conn, "old", "the deploy target is us-east-1", _SAME, OLD_TS)
    _insert(conn, "new", "the deploy target is eu-west-2", _SAME, NEW_TS)
    _insert(conn, "other", "prefers tabs over spaces", _DIFF, MID_TS)


# ---------------------------------------------------------------------------
# Detection — read-only, correct direction, no false positives
# ---------------------------------------------------------------------------


class TestFindCandidates:
    def test_flags_contradictory_pair_with_correct_direction(
        self, db_conn: sqlite3.Connection
    ) -> None:
        _seed_contradiction(db_conn)
        cands = find_reconciliation_candidates(db_conn)
        assert len(cands) == 1
        c = cands[0]
        assert c.newer_id == "new"  # more recent updated_at is kept
        assert c.older_id == "old"
        assert c.similarity >= 0.75

    def test_dissimilar_pairs_not_flagged(self, db_conn: sqlite3.Connection) -> None:
        _insert(db_conn, "a", "x", _SAME, OLD_TS)
        _insert(db_conn, "b", "y", _DIFF, NEW_TS)
        assert find_reconciliation_candidates(db_conn) == []

    def test_equal_created_at_not_flagged(self, db_conn: sqlite3.Connection) -> None:
        # High similarity but no clear newer → not a supersede candidate.
        _insert(db_conn, "a", "x", _SAME, OLD_TS)
        _insert(db_conn, "b", "y", _SAME, OLD_TS)
        assert find_reconciliation_candidates(db_conn) == []

    def test_aging_bumped_updated_at_does_not_invert(
        self, db_conn: sqlite3.Connection
    ) -> None:
        """Direction must survive the aging cycle. bulk_update_importance bumps
        updated_at on not-accessed-today rows — using updated_at would invert
        stale/current. created_at is immutable, so the kept memory stays the one
        that was genuinely created later."""
        from claude_memory.db.queries import bulk_update_importance

        _insert(db_conn, "old", "target east", _SAME, OLD_TS)   # created Jan
        _insert(db_conn, "new", "target west", _SAME, NEW_TS)   # created Jun
        bulk_update_importance(db_conn, decay_rate=0.99)  # churns updated_at
        cands = find_reconciliation_candidates(db_conn)
        assert len(cands) == 1
        assert cands[0].newer_id == "new"   # created later → kept
        assert cands[0].older_id == "old"

    def test_pinned_excluded(self, db_conn: sqlite3.Connection) -> None:
        _insert(db_conn, "old", "target a", _SAME, OLD_TS, tags=["forever-keep"])
        _insert(db_conn, "new", "target b", _SAME, NEW_TS)
        assert find_reconciliation_candidates(db_conn) == []

    def test_detection_does_not_mutate(self, db_conn: sqlite3.Connection) -> None:
        """No auto-supersede: detection must leave tiers and supersedes untouched."""
        _seed_contradiction(db_conn)
        find_reconciliation_candidates(db_conn)
        assert get_memory(db_conn, "old").tier == "hot"
        assert get_memory(db_conn, "new").supersedes is None

    def test_already_reconciled_pair_skipped(
        self, db_conn: sqlite3.Connection
    ) -> None:
        _seed_contradiction(db_conn)
        update_memory(db_conn, "new", supersedes="old")  # already linked
        assert find_reconciliation_candidates(db_conn) == []


# ---------------------------------------------------------------------------
# Apply — supersede + archive, never hard-delete, guarded
# ---------------------------------------------------------------------------


class TestApplyReconciliation:
    def test_supersedes_and_archives_older(
        self, db_conn: sqlite3.Connection
    ) -> None:
        _seed_contradiction(db_conn)
        result = apply_reconciliation(db_conn, "new", "old")
        assert result["reconciled"] is True
        newer = get_memory(db_conn, "new")
        older = get_memory(db_conn, "old")
        assert newer.supersedes == "old"      # lineage set on the kept memory
        assert older is not None               # NOT hard-deleted
        assert older.tier == "archived"        # older archived
        assert newer.tier == "hot"             # newer untouched tier

    def test_inverted_direction_rejected(
        self, db_conn: sqlite3.Connection
    ) -> None:
        """Passing the older as newer_id must be refused (never archive the newer)."""
        _seed_contradiction(db_conn)
        with pytest.raises(ReconciliationError):
            apply_reconciliation(db_conn, "old", "new")  # inverted
        # Nothing changed.
        assert get_memory(db_conn, "new").tier == "hot"
        assert get_memory(db_conn, "old").tier == "hot"

    def test_self_pair_rejected(self, db_conn: sqlite3.Connection) -> None:
        _seed_contradiction(db_conn)
        with pytest.raises(ReconciliationError):
            apply_reconciliation(db_conn, "new", "new")

    def test_unknown_id_rejected(self, db_conn: sqlite3.Connection) -> None:
        _seed_contradiction(db_conn)
        with pytest.raises(ReconciliationError):
            apply_reconciliation(db_conn, "new", "ghost")

    def test_pinned_rejected(self, db_conn: sqlite3.Connection) -> None:
        _insert(db_conn, "old", "a", _SAME, OLD_TS, tags=["forever-keep"])
        _insert(db_conn, "new", "b", _SAME, NEW_TS)
        with pytest.raises(ReconciliationError):
            apply_reconciliation(db_conn, "new", "old")

    def test_already_archived_older_rejected(
        self, db_conn: sqlite3.Connection
    ) -> None:
        _seed_contradiction(db_conn)
        update_memory(db_conn, "old", tier="archived")
        with pytest.raises(ReconciliationError):
            apply_reconciliation(db_conn, "new", "old")

    def test_archived_newer_rejected(self, db_conn: sqlite3.Connection) -> None:
        """Keeping an archived (invisible) memory while archiving a live one must
        be refused — else the live content leaves retrieval with no successor."""
        _insert(db_conn, "arch", "a", _SAME, NEW_TS, tier="archived")
        _insert(db_conn, "live", "b", _SAME, OLD_TS, tier="hot")
        with pytest.raises(ReconciliationError):
            apply_reconciliation(db_conn, "arch", "live")
        assert get_memory(db_conn, "live").tier == "hot"  # untouched

    def test_clobbering_supersedes_rejected(
        self, db_conn: sqlite3.Connection
    ) -> None:
        """A second reconciliation on the same kept memory must not silently
        overwrite its existing supersedes link (single-valued → lineage loss)."""
        _insert(db_conn, "n", "current", _SAME, NEW_TS)
        _insert(db_conn, "o1", "stale1", _SAME, MID_TS)
        _insert(db_conn, "o2", "stale2", _SAME, OLD_TS)
        apply_reconciliation(db_conn, "n", "o1")  # n supersedes o1
        with pytest.raises(ReconciliationError):
            apply_reconciliation(db_conn, "n", "o2")
        assert get_memory(db_conn, "n").supersedes == "o1"  # link preserved
        assert get_memory(db_conn, "o2").tier == "hot"      # not archived


# ---------------------------------------------------------------------------
# rerank excludes archived (the retrieval-gap fix A3 depends on)
# ---------------------------------------------------------------------------


class TestRerankExcludesArchived:
    def test_archived_candidate_dropped(self, db_conn: sqlite3.Connection) -> None:
        _insert(db_conn, "live", "x", _SAME, OLD_TS, tier="hot")
        _insert(db_conn, "gone", "y", _SAME, OLD_TS, tier="archived")
        records = {
            "live": get_memory(db_conn, "live"),
            "gone": get_memory(db_conn, "gone"),
        }
        cands = [
            RetrievalCandidate(memory_id="live", vec_distance=0.0),
            RetrievalCandidate(memory_id="gone", vec_distance=0.0),
        ]
        ids = {s.memory_id for s in rerank(cands, records)}
        assert "live" in ids
        assert "gone" not in ids

    def test_archived_excluded_even_if_always_load(
        self, db_conn: sqlite3.Connection
    ) -> None:
        _insert(db_conn, "gone", "y", _SAME, OLD_TS, tier="archived")
        records = {"gone": get_memory(db_conn, "gone")}
        cands = [RetrievalCandidate(memory_id="gone", is_always_load=True)]
        assert rerank(cands, records) == []


# ---------------------------------------------------------------------------
# Integration — older leaves search + browse; lineage resolves
# ---------------------------------------------------------------------------


class TestReconciliationIntegration:
    def test_older_stops_appearing_in_search(
        self, db_conn: sqlite3.Connection, mock_encoder, monkeypatch
    ) -> None:
        # search_memories bound its own search_vec reference at import; point it
        # at the brute-force stand-in (conftest only patches the queries copy).
        from tests.conftest import brute_force_vec_search

        monkeypatch.setattr(
            "claude_memory.retrieval.search.search_vec",
            lambda c, emb, top_k=50: brute_force_vec_search(c, emb, top_k),
        )
        _insert(db_conn, "old", "zzqtoken deploy target east", _SAME, OLD_TS)
        _insert(db_conn, "new", "zzqtoken deploy target west", _SAME, NEW_TS)

        before = {
            r.memory.id
            for r in search_memories(
                db_conn, mock_encoder, query="zzqtoken",
                update_access_on_retrieve=False,
            )
        }
        assert "old" in before  # retrievable before reconciliation

        apply_reconciliation(db_conn, "new", "old")

        after = {
            r.memory.id
            for r in search_memories(
                db_conn, mock_encoder, query="zzqtoken",
                update_access_on_retrieve=False,
            )
        }
        assert "old" not in after   # archived → excluded from search
        assert "new" in after       # the kept memory still surfaces

    def test_older_leaves_default_browse(self, db_conn: sqlite3.Connection) -> None:
        _seed_contradiction(db_conn)
        apply_reconciliation(db_conn, "new", "old")
        assert "old" not in {r.id for r in list_memories(db_conn)}

    def test_lineage_resolves_via_memory_why(
        self, db_conn: sqlite3.Connection
    ) -> None:
        _seed_contradiction(db_conn)
        apply_reconciliation(db_conn, "new", "old")
        why = _memory_why_dict(get_memory(db_conn, "new"))
        assert why["supersedes"] == "old"


# ---------------------------------------------------------------------------
# REST routes + tool layer
# ---------------------------------------------------------------------------


class TestReconciliationRoutes:
    def _patch(self, monkeypatch, db_conn: sqlite3.Connection) -> None:
        monkeypatch.setattr(
            "claude_memory.mcp.tools._get_deps",
            lambda: (_NoCloseConn(db_conn), None, None),
        )

    async def test_candidates_route_lists_pair(
        self, db_conn: sqlite3.Connection, monkeypatch
    ) -> None:
        _seed_contradiction(db_conn)
        self._patch(monkeypatch, db_conn)
        out = await reconciliation_candidates(limit=100, similarity_threshold=0.75)
        assert out["count"] == 1
        cand = out["candidates"][0]
        assert cand["newer"]["id"] == "new"
        assert cand["older"]["id"] == "old"
        assert "preview" in cand["newer"] and "content" not in cand["newer"]

    async def test_apply_route_reconciles(
        self, db_conn: sqlite3.Connection, monkeypatch
    ) -> None:
        _seed_contradiction(db_conn)
        self._patch(monkeypatch, db_conn)
        result = await reconciliation_apply(
            ReconcileApplyRequest(newer_id="new", older_id="old")
        )
        assert result["reconciled"] is True
        assert get_memory(db_conn, "old").tier == "archived"
        assert get_memory(db_conn, "new").supersedes == "old"

    async def test_apply_route_inverted_is_400(
        self, db_conn: sqlite3.Connection, monkeypatch
    ) -> None:
        _seed_contradiction(db_conn)
        self._patch(monkeypatch, db_conn)
        with pytest.raises(HTTPException) as exc:
            await reconciliation_apply(
                ReconcileApplyRequest(newer_id="old", older_id="new")
            )
        assert exc.value.status_code == 400

    async def test_tool_apply_commits(
        self, db_conn: sqlite3.Connection, monkeypatch
    ) -> None:
        _seed_contradiction(db_conn)
        self._patch(monkeypatch, db_conn)
        await tool_reconciliation_apply(newer_id="new", older_id="old")
        assert get_memory(db_conn, "old").tier == "archived"

    async def test_tool_candidates_read_only(
        self, db_conn: sqlite3.Connection, monkeypatch
    ) -> None:
        _seed_contradiction(db_conn)
        self._patch(monkeypatch, db_conn)
        out = await tool_reconciliation_candidates()
        assert out["count"] == 1
        # read-only: still not superseded
        assert get_memory(db_conn, "new").supersedes is None
