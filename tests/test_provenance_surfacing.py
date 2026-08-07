"""Tests for Tier A / A2 — provenance + retrieval-stats surfacing (WU-1).

Covers the four surfaces WU-1 widens and the adversarial hunt list (§6 of the
orchestrator):

* serializer parity — the full and summary paths expose the *same* provenance
  set, with real (non-null) values, so surfacing is not a silent no-op;
* REST-model coercion — ``SearchResultItem`` actually keeps the provenance
  fields (FastAPI drops any serializer key the model doesn't declare);
* ``get_stats`` aggregate math — ``never_retrieved`` / ``unscoped`` counts and
  the ``top_n_share`` denominator (all retrievals, never the memory count);
* ``memory_why`` — returns lineage, **excludes content**, and does **not** bump
  ``access_count`` (inspecting must not count as retrieving).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from claude_memory.api.routes import SearchResultItem, StatsResponse
from claude_memory.api.routes import why as why_route
from claude_memory.db.queries import MemoryRecord, get_stats, insert_memory
from claude_memory.mcp.tools import (
    _memory_why_dict,
    _search_result_to_dict,
    _search_result_to_summary_dict,
    tool_memory_why,
)
from claude_memory.retrieval.search import SearchResult

# The four provenance signals WU-1 surfaces (design doc §A2.1).
PROVENANCE_FIELDS = {"access_count", "last_accessed", "source_session", "project_dir"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    *,
    id: str = "prov-001",
    content: str = "some memory content",
    access_count: int = 3,
    project_dir: str | None = "/projects/Example.App",
    source_session: str | None = "sess-abc-123",
    supersedes: str | None = None,
    consolidated_from: list[str] | None = None,
    last_accessed: str | None = None,
) -> MemoryRecord:
    now = datetime.now(timezone.utc)
    return MemoryRecord(
        id=id,
        content=content,
        summary=None,
        type="lesson",
        tags=["kind:tooling"],
        created_at=(now - timedelta(days=2)).isoformat(),
        updated_at=(now - timedelta(days=1)).isoformat(),
        last_accessed=last_accessed or now.isoformat(),
        access_count=access_count,
        importance=7.5,
        tier="hot",
        project_dir=project_dir,
        source_session=source_session,
        supersedes=supersedes,
        consolidated_from=consolidated_from or [],
        metadata={},
    )


def _make_result(**kw) -> SearchResult:
    return SearchResult(
        memory=_make_record(**kw),
        score=0.85,
        semantic_score=0.80,
        recency_score=0.90,
        frequency_score=0.50,
        importance_score=0.75,
        lexical_score=0.60,
    )


def _dummy_embedding(dim: int = 384) -> list[float]:
    return [0.1] * dim


class _NoCloseConn:
    """Proxy that forwards to a real connection but no-ops ``close()``.

    ``tool_memory_why`` closes its connection in a ``finally`` block; when we
    hand it the fixture's in-memory connection we must keep that connection
    alive so post-call assertions (e.g. "access_count did not change") can
    still query it.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:  # noqa: D401 - intentional no-op
        pass


# ---------------------------------------------------------------------------
# Serializer parity — provenance present in BOTH paths, no silent no-op
# ---------------------------------------------------------------------------


class TestSerializerProvenanceParity:
    def test_full_dict_includes_all_provenance_fields(self) -> None:
        d = _search_result_to_dict(_make_result())
        for field in PROVENANCE_FIELDS:
            assert field in d, f"full serializer missing {field}"
        assert d["content"] == "some memory content"  # full path keeps content

    def test_summary_dict_includes_all_provenance_fields(self) -> None:
        d = _search_result_to_summary_dict(_make_result())
        for field in PROVENANCE_FIELDS:
            assert field in d, f"summary serializer missing {field}"
        assert "content" not in d  # summary path still omits content

    def test_both_paths_agree_on_provenance_values(self) -> None:
        """Parity: identical provenance keys AND identical values."""
        result = _make_result()
        full = _search_result_to_dict(result)
        summary = _search_result_to_summary_dict(result)
        for field in PROVENANCE_FIELDS:
            assert full[field] == summary[field], f"parity gap on {field}"

    def test_values_are_real_not_null(self) -> None:
        """Guards the 'widened field silently returns null' failure mode."""
        d = _search_result_to_dict(
            _make_result(access_count=9, source_session="sess-xyz")
        )
        assert d["access_count"] == 9
        assert d["source_session"] == "sess-xyz"
        assert d["project_dir"] == "/projects/Example.App"
        assert d["last_accessed"]  # non-empty timestamp


# ---------------------------------------------------------------------------
# REST model coercion — SearchResultItem must KEEP the provenance fields
# ---------------------------------------------------------------------------


class TestSearchResultItemModel:
    def test_model_preserves_provenance_from_full_dict(self) -> None:
        """Simulates the FastAPI route: dict -> SearchResultItem coercion.

        Extra serializer keys (semantic_score, ...) are ignored by pydantic;
        the provenance fields must survive because the model now declares them.
        """
        d = _search_result_to_dict(
            _make_result(access_count=11, source_session="sess-keep")
        )
        item = SearchResultItem(**d)
        assert item.access_count == 11
        assert item.source_session == "sess-keep"
        assert item.project_dir == "/projects/Example.App"
        assert item.last_accessed

    def test_nullable_provenance_defaults(self) -> None:
        d = _search_result_to_dict(
            _make_result(project_dir=None, source_session=None)
        )
        item = SearchResultItem(**d)
        assert item.project_dir is None
        assert item.source_session is None


# ---------------------------------------------------------------------------
# get_stats — new aggregates and their arithmetic
# ---------------------------------------------------------------------------


class TestGetStatsAggregates:
    def test_never_retrieved_and_unscoped_counts(
        self, db_conn: sqlite3.Connection, sample_memories: list[MemoryRecord]
    ) -> None:
        stats = get_stats(db_conn)
        # mem-008 and mem-009 have access_count == 0.
        assert stats["never_retrieved"] == 2
        # Only mem-010 carries a project_dir; the other 9 are unscoped.
        assert stats["unscoped"] == 9

    def test_never_retrieved_matches_ground_truth_sql(
        self, db_conn: sqlite3.Connection, sample_memories: list[MemoryRecord]
    ) -> None:
        ground_truth = db_conn.execute(
            "SELECT COUNT(*) AS c FROM memories WHERE access_count = 0"
        ).fetchone()["c"]
        assert get_stats(db_conn)["never_retrieved"] == ground_truth

    def test_top_n_share_denominator_is_total_retrievals(
        self, db_conn: sqlite3.Connection, sample_memories: list[MemoryRecord]
    ) -> None:
        # access counts: 15,10,20,5,3,2,1,0,0,8 -> sum 64. Top-2 = 20+15 = 35.
        stats = get_stats(db_conn, top_n=2)
        assert stats["top_n_share"] == pytest.approx(35 / 64, rel=1e-4)

    def test_top_n_share_all_fit_when_n_exceeds_corpus(
        self, db_conn: sqlite3.Connection, sample_memories: list[MemoryRecord]
    ) -> None:
        # Default top_n=15 > 10 memories, so the head == the whole corpus -> 1.0.
        assert get_stats(db_conn)["top_n_share"] == pytest.approx(1.0)

    def test_empty_db_is_safe(self, db_conn: sqlite3.Connection) -> None:
        stats = get_stats(db_conn)
        assert stats["never_retrieved"] == 0
        assert stats["unscoped"] == 0
        assert stats["top_n_share"] == 0.0  # no divide-by-zero

    def test_all_zero_access_guards_denominator(
        self, db_conn: sqlite3.Connection
    ) -> None:
        for i in range(3):
            insert_memory(
                db_conn,
                _make_record(id=f"z-{i}", access_count=0, project_dir=None),
                _dummy_embedding(),
            )
        stats = get_stats(db_conn)
        assert stats["never_retrieved"] == 3
        assert stats["top_n_share"] == 0.0  # sum(access)==0 -> guarded, not crash

    def test_existing_keys_unchanged(
        self, db_conn: sqlite3.Connection, sample_memories: list[MemoryRecord]
    ) -> None:
        stats = get_stats(db_conn)
        assert stats["total"] == 10
        assert stats["by_type"]["user"] == 2
        assert stats["by_tier"]["hot"] == 4


class TestStatsResponseModel:
    def test_model_accepts_extended_stats(
        self, db_conn: sqlite3.Connection, sample_memories: list[MemoryRecord]
    ) -> None:
        resp = StatsResponse(**get_stats(db_conn))
        assert resp.never_retrieved == 2
        assert resp.unscoped == 9
        assert 0.0 <= resp.top_n_share <= 1.0


# ---------------------------------------------------------------------------
# _memory_why_dict — pure shape (lineage in, content out)
# ---------------------------------------------------------------------------


class TestMemoryWhyDict:
    def test_includes_lineage_fields(self) -> None:
        d = _memory_why_dict(
            _make_record(supersedes="old-1", consolidated_from=["c-1", "c-2"])
        )
        for field in (
            "id",
            "source_session",
            "created_at",
            "last_accessed",
            "access_count",
            "supersedes",
            "consolidated_from",
        ):
            assert field in d, f"missing lineage field: {field}"
        assert d["supersedes"] == "old-1"
        assert d["consolidated_from"] == ["c-1", "c-2"]

    def test_excludes_content(self) -> None:
        d = _memory_why_dict(_make_record(content="secret content"))
        assert "content" not in d
        assert "secret content" not in str(d.values())

    def test_passes_through_real_list(self) -> None:
        d = _memory_why_dict(_make_record(consolidated_from=["p1", "p2"]))
        assert d["consolidated_from"] == ["p1", "p2"]

    def test_normalizes_double_encoded_string(self) -> None:
        """consolidation-written rows round-trip consolidated_from as a JSON
        *string*; the helper must hand back a real list, not a string that a
        consumer would iterate character-by-character."""
        rec = _make_record()
        # Simulate the post-_row_to_record state for a consolidation row.
        rec.consolidated_from = json.dumps(["parent-a", "parent-b"])  # a str
        d = _memory_why_dict(rec)
        assert d["consolidated_from"] == ["parent-a", "parent-b"]
        assert isinstance(d["consolidated_from"], list)


# ---------------------------------------------------------------------------
# Regression: consolidation-written lineage (the double-encode class) through
# the real insert -> get_memory -> memory_why round-trip (Finding 1/2).
# ---------------------------------------------------------------------------


class TestConsolidatedFromRoundTrip:
    async def test_tool_returns_list_for_consolidation_written_row(
        self, db_conn: sqlite3.Connection, monkeypatch
    ) -> None:
        # Exactly how run_consolidation writes it: consolidated_from is a
        # json.dumps'd string BEFORE insert_memory dumps it a second time.
        rec = _make_record(id="consol-1")
        rec.consolidated_from = json.dumps(["cold-0", "cold-1", "cold-2"])
        insert_memory(db_conn, rec, _dummy_embedding())

        # Confirm the stored/round-tripped column is genuinely a string here
        # (i.e. this test would catch a regression only if normalization works).
        from claude_memory.db.queries import get_memory

        assert isinstance(get_memory(db_conn, "consol-1").consolidated_from, str)

        monkeypatch.setattr(
            "claude_memory.mcp.tools._get_deps",
            lambda: (_NoCloseConn(db_conn), None, None),
        )
        result = await tool_memory_why("consol-1")
        assert result["consolidated_from"] == ["cold-0", "cold-1", "cold-2"]
        assert isinstance(result["consolidated_from"], list)


# ---------------------------------------------------------------------------
# tool_memory_why — end-to-end via patched deps (read-only, no access bump)
# ---------------------------------------------------------------------------


class TestToolMemoryWhy:
    def _patch_deps(self, monkeypatch, db_conn: sqlite3.Connection) -> None:
        monkeypatch.setattr(
            "claude_memory.mcp.tools._get_deps",
            lambda: (_NoCloseConn(db_conn), None, None),
        )

    async def test_returns_lineage_for_known_id(
        self, db_conn: sqlite3.Connection, monkeypatch
    ) -> None:
        insert_memory(
            db_conn,
            _make_record(id="why-1", source_session="sess-known", access_count=4),
            _dummy_embedding(),
        )
        self._patch_deps(monkeypatch, db_conn)

        result = await tool_memory_why("why-1")

        assert result["found"] is True
        assert result["id"] == "why-1"
        assert result["source_session"] == "sess-known"
        assert result["access_count"] == 4
        assert "content" not in result  # never echo content

    async def test_does_not_bump_access_count(
        self, db_conn: sqlite3.Connection, monkeypatch
    ) -> None:
        insert_memory(
            db_conn,
            _make_record(id="why-2", access_count=5),
            _dummy_embedding(),
        )
        self._patch_deps(monkeypatch, db_conn)

        await tool_memory_why("why-2")

        # The connection is still open (proxy no-ops close); read it back.
        row = db_conn.execute(
            "SELECT access_count FROM memories WHERE id = ?", ("why-2",)
        ).fetchone()
        assert row["access_count"] == 5  # inspecting is NOT retrieving

    async def test_unknown_id_returns_not_found(
        self, db_conn: sqlite3.Connection, monkeypatch
    ) -> None:
        self._patch_deps(monkeypatch, db_conn)
        result = await tool_memory_why("ghost")
        assert result["found"] is False
        assert result["id"] == "ghost"


# ---------------------------------------------------------------------------
# GET /api/v1/memories/{id}/why route contract (200 + lineage / 404)
# ---------------------------------------------------------------------------


class TestWhyRoute:
    def _patch_deps(self, monkeypatch, db_conn: sqlite3.Connection) -> None:
        monkeypatch.setattr(
            "claude_memory.mcp.tools._get_deps",
            lambda: (_NoCloseConn(db_conn), None, None),
        )

    async def test_route_returns_lineage(
        self, db_conn: sqlite3.Connection, monkeypatch
    ) -> None:
        insert_memory(
            db_conn,
            _make_record(id="route-1", source_session="sess-route"),
            _dummy_embedding(),
        )
        self._patch_deps(monkeypatch, db_conn)

        body = await why_route("route-1")

        assert body["found"] is True
        assert body["source_session"] == "sess-route"
        assert "content" not in body

    async def test_route_raises_404_for_unknown(
        self, db_conn: sqlite3.Connection, monkeypatch
    ) -> None:
        self._patch_deps(monkeypatch, db_conn)
        with pytest.raises(HTTPException) as exc:
            await why_route("ghost")
        assert exc.value.status_code == 404
