"""Tests for strict tags[] AND filter in search_memories (issues #8, #10)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_memory.db.queries import MemoryRecord, insert_memory
from claude_memory.db.schema import initialize_db
from claude_memory.retrieval.search import (
    _CANDIDATE_POOL_MULTIPLIER,
    _apply_tag_filter,
    search_memories,
)

from tests.conftest import MockEncoder


def _now():
    return datetime.now(timezone.utc)


def _make_record(
    id: str,
    tags: list[str],
    project_dir: str | None = None,
    importance: float = 5.0,
) -> MemoryRecord:
    now = _now()
    return MemoryRecord(
        id=id,
        content=f"Content for {id}",
        summary=None,
        type="lesson",
        tags=tags,
        created_at=(now - timedelta(days=1)).isoformat(),
        updated_at=now.isoformat(),
        last_accessed=now.isoformat(),
        access_count=1,
        importance=importance,
        tier="hot",
        project_dir=project_dir,
        source_session=None,
        supersedes=None,
        consolidated_from=[],
        metadata={},
    )


class TestApplyTagFilter:
    def _records(self) -> dict[str, MemoryRecord]:
        return {
            "m1": _make_record("m1", ["kind:tripwire", "lifecycle:active"]),
            "m2": _make_record("m2", ["kind:tripwire", "lifecycle:resolved"]),
            "m3": _make_record("m3", ["kind:lesson", "lifecycle:active"]),
            "m4": _make_record("m4", ["kind:tripwire", "lifecycle:active", "scope:cookbook"]),
            "m5": _make_record("m5", []),  # no tags
        }

    def test_no_tags_returns_all(self):
        records = self._records()
        result = _apply_tag_filter(records, required_tags=None)
        assert set(result.keys()) == {"m1", "m2", "m3", "m4", "m5"}

    def test_empty_tags_returns_all(self):
        records = self._records()
        result = _apply_tag_filter(records, required_tags=[])
        assert set(result.keys()) == {"m1", "m2", "m3", "m4", "m5"}

    def test_single_tag_filter(self):
        records = self._records()
        result = _apply_tag_filter(records, required_tags=["kind:tripwire"])
        assert set(result.keys()) == {"m1", "m2", "m4"}

    def test_and_filter_both_required(self):
        records = self._records()
        result = _apply_tag_filter(records, required_tags=["kind:tripwire", "lifecycle:active"])
        assert set(result.keys()) == {"m1", "m4"}

    def test_superset_tags_pass(self):
        records = self._records()
        result = _apply_tag_filter(
            records, required_tags=["kind:tripwire", "lifecycle:active", "scope:cookbook"]
        )
        assert set(result.keys()) == {"m4"}

    def test_no_match_returns_empty(self):
        records = self._records()
        result = _apply_tag_filter(records, required_tags=["kind:nonexistent"])
        assert result == {}

    def test_empty_tags_on_record_excluded(self):
        records = self._records()
        result = _apply_tag_filter(records, required_tags=["kind:tripwire"])
        assert "m5" not in result

    def test_json_encoded_tags_supported(self):
        """Records from DB may have tags as a JSON string — filter must handle both."""
        now = _now()
        record = MemoryRecord(
            id="json-tags",
            content="test",
            summary=None,
            type="lesson",
            tags=json.dumps(["kind:tripwire", "lifecycle:active"]),
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            last_accessed=now.isoformat(),
            access_count=0,
            importance=5.0,
            tier="hot",
            project_dir=None,
            source_session=None,
            supersedes=None,
            consolidated_from=[],
            metadata={},
        )
        result = _apply_tag_filter(
            {"json-tags": record}, required_tags=["kind:tripwire"]
        )
        assert "json-tags" in result


# ---------------------------------------------------------------------------
# Integration: the candidate-pool bound must not run before the tag filter
# ---------------------------------------------------------------------------
#
# _apply_tag_filter alone (above) only proves the filter is correct once it
# has the full candidate set in hand. It cannot catch a bug where the
# candidate set was truncated to top_k * _CANDIDATE_POOL_MULTIPLIER before
# the filter ever ran — a required-tag row ranked below that bound is simply
# never fetched from the database, so no amount of filter correctness can
# recover it. These tests exercise the real search_memories() pipeline to
# catch that class of false negative.


class TestTagFilterSurvivesPoolBound:
    def test_low_ranked_tagged_memory_survives_at_top_k_one(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """A required-tag row ranked below the pool bound must still return.

        Five decoys outrank "wanted" on importance alone. At top_k=1 the
        candidate pool is bounded to 3 (top_k * _CANDIDATE_POOL_MULTIPLIER).
        If that bound is applied before the tags[] filter, "wanted" is
        evicted from the pool before its tag is ever checked, and the
        caller sees an empty result indistinguishable from "no match
        exists" — even though a row satisfying the filter is in the corpus.
        """
        for i in range(5):
            decoy = _make_record(f"decoy-{i}", tags=["decoy"], importance=9.0)
            insert_memory(db_conn, decoy, mock_encoder.encode(decoy.content))

        wanted = _make_record("wanted", tags=["wanted-7"], importance=7.0)
        insert_memory(db_conn, wanted, mock_encoder.encode(wanted.content))
        db_conn.commit()

        with (
            patch("claude_memory.retrieval.search.search_vec", return_value=[]),
            patch("claude_memory.retrieval.search.search_fts", return_value=[]),
        ):
            results = search_memories(
                db_conn,
                mock_encoder,
                query="irrelevant query text",
                top_k=1,
                tags=["wanted-7"],
                update_access_on_retrieve=False,
            )

        assert [r.memory.id for r in results] == ["wanted"]

    def test_low_ranked_tagged_memory_survives_via_vector_pool(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """Same defect, demonstrated through the vector-search source.

        search_vec takes required_tags directly and applies it as a true
        id-eligibility pre-filter inside its own SQL — an ``id IN
        (subquery)`` constraint vec0 recognises before computing nearest
        neighbours, not a caller-side widened top_k (see search_vec's
        docstring and the real-sqlite-vec evidence script from the session
        report; a magic widened-ceiling approach was tried first and
        rejected — a tagged candidate ranked beyond the ceiling would still
        be silently evicted). This verifies search.py threads `tags`
        through to search_vec's `required_tags`, and that — via a fake
        reproducing that pre-filter-then-bound contract — a low-ranked
        tagged memory survives a small, *unwidened* pool bound despite five
        closer untagged decoys.
        """
        wanted = _make_record("vec-wanted", tags=["wanted-7"])
        insert_memory(db_conn, wanted, mock_encoder.encode(wanted.content))
        for i in range(5):
            decoy = _make_record(f"vec-decoy-{i}", tags=["decoy"])
            insert_memory(db_conn, decoy, mock_encoder.encode(decoy.content))
        db_conn.commit()

        # Decoys rank closer (lower distance) than "wanted" — ordered exactly
        # as a real KNN scan would rank them.
        ranked_vec_hits: list[tuple[str, float]] = [
            (f"vec-decoy-{i}", 0.01 + i * 0.001) for i in range(5)
        ]
        ranked_vec_hits.append(("vec-wanted", 0.90))
        tags_by_id: dict[str, list[str]] = {"vec-wanted": ["wanted-7"]}
        tags_by_id.update({f"vec-decoy-{i}": ["decoy"] for i in range(5)})

        def fake_search_vec(
            conn: sqlite3.Connection,
            embedding: list[float],
            top_k: int = 50,
            project_dir: str | None = None,
            recall_scope: bool = False,
            required_tags: list[str] | None = None,
        ) -> list[tuple[str, float]]:
            pool = ranked_vec_hits
            if required_tags:
                required = set(required_tags)
                pool = [
                    (mid, dist) for mid, dist in pool
                    if required.issubset(set(tags_by_id.get(mid, [])))
                ]
            return pool[:top_k]

        with (
            patch(
                "claude_memory.retrieval.search.search_vec",
                side_effect=fake_search_vec,
            ) as vec_spy,
            patch("claude_memory.retrieval.search.search_fts", return_value=[]),
        ):
            results = search_memories(
                db_conn,
                mock_encoder,
                query="irrelevant query text",
                top_k=1,
                tags=["wanted-7"],
                update_access_on_retrieve=False,
            )

        assert [r.memory.id for r in results] == ["vec-wanted"]
        assert vec_spy.call_args.kwargs["required_tags"] == ["wanted-7"]
        # The bound itself never widens — correctness now comes entirely
        # from the pre-filter, not from over-fetching.
        assert vec_spy.call_args.kwargs["top_k"] == 3

    def test_low_ranked_tagged_memory_survives_via_fts_pool(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """Same defect, demonstrated through the full-text-search source."""
        wanted = _make_record("fts-wanted", tags=["wanted-7"])
        insert_memory(db_conn, wanted, mock_encoder.encode(wanted.content))
        for i in range(5):
            decoy = _make_record(f"fts-decoy-{i}", tags=["decoy"])
            insert_memory(db_conn, decoy, mock_encoder.encode(decoy.content))
        db_conn.commit()

        # Decoys rank better (lower BM25 rank) than "wanted".
        ranked_fts_hits: list[tuple[str, float]] = [
            (f"fts-decoy-{i}", float(i)) for i in range(5)
        ]
        ranked_fts_hits.append(("fts-wanted", 90.0))

        def fake_search_fts(
            conn: sqlite3.Connection,
            query: str,
            project_dir: str | None = None,
            top_k: int = 50,
            recall_scope: bool = False,
        ) -> list[tuple[str, float]]:
            return ranked_fts_hits[:top_k]

        with (
            patch("claude_memory.retrieval.search.search_vec", return_value=[]),
            patch(
                "claude_memory.retrieval.search.search_fts",
                side_effect=fake_search_fts,
            ),
        ):
            results = search_memories(
                db_conn,
                mock_encoder,
                query="irrelevant query text",
                top_k=1,
                tags=["wanted-7"],
                update_access_on_retrieve=False,
            )

        assert [r.memory.id for r in results] == ["fts-wanted"]

    @pytest.mark.parametrize("top_k", [2, 3])
    def test_regression_at_production_hook_top_k_values(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
        top_k: int,
    ) -> None:
        """The defect's production trigger, named directly in the finding:
        real hooks call search_memories with top_k=2 or top_k=3 — caps of 6
        and 9 respectively.
        """
        cap: int = top_k * _CANDIDATE_POOL_MULTIPLIER
        for i in range(cap + 2):
            decoy = _make_record(f"hook-decoy-{i}", tags=["decoy"], importance=9.0)
            insert_memory(db_conn, decoy, mock_encoder.encode(decoy.content))
        wanted = _make_record("hook-wanted", tags=["wanted-7"], importance=7.0)
        insert_memory(db_conn, wanted, mock_encoder.encode(wanted.content))
        db_conn.commit()

        with (
            patch("claude_memory.retrieval.search.search_vec", return_value=[]),
            patch("claude_memory.retrieval.search.search_fts", return_value=[]),
        ):
            results = search_memories(
                db_conn,
                mock_encoder,
                query="irrelevant query text",
                top_k=top_k,
                tags=["wanted-7"],
                update_access_on_retrieve=False,
            )

        assert "hook-wanted" in [r.memory.id for r in results]

    def test_no_tags_keeps_the_pool_bound(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """Without tags[], the pool must stay bounded — no regression to
        issue #29 (an unbounded always-load pool flooding a mature corpus).
        """
        for i in range(10):
            record = _make_record(f"mem-{i}", tags=[], importance=9.0)
            insert_memory(db_conn, record, mock_encoder.encode(record.content))
        db_conn.commit()

        with (
            patch("claude_memory.retrieval.search.search_vec", return_value=[]),
            patch("claude_memory.retrieval.search.search_fts", return_value=[]),
            patch(
                "claude_memory.retrieval.search.get_always_load",
            ) as always_load_spy,
        ):
            always_load_spy.return_value = []
            search_memories(
                db_conn,
                mock_encoder,
                query="irrelevant query text",
                top_k=1,
                update_access_on_retrieve=False,
            )

        # top_k=1 * _CANDIDATE_POOL_MULTIPLIER=3 — unchanged when no tags
        # filter is supplied, since there is nothing to widen the pool for.
        assert always_load_spy.call_args.kwargs["limit"] == 3


# ---------------------------------------------------------------------------
# Real sqlite-vec: the pushed-down tag pre-filter must equal the strict filter
# ---------------------------------------------------------------------------
#
# The tests above stub search_vec, so they cannot see what the tag predicate
# inside search_vec's own SQL does. That predicate used to be a
# ``tags LIKE '%"<tag>"%'`` chain over the JSON text, which differs from the
# strict caller-side filter (exact, case-sensitive subset) in both directions:
# LIKE wildcards / case-folding admit decoys that fill the vec0 LIMIT before
# the strict filter runs, and ``json.dumps`` escaping (ensure_ascii) hides
# non-ASCII, ``"`` and ``\`` from LIKE altogether. These tests run the real
# vec0 KNN so the pre-filter is what is actually under test.


def _unit(axis: int, dim: int = 384) -> list[float]:
    vec: list[float] = [0.0] * dim
    vec[axis] = 1.0
    return vec


def _near(axis: int, nudge: float, dim: int = 384) -> list[float]:
    """A vector close to axis 0, nudged toward *axis* (larger = farther)."""
    vec: list[float] = [0.0] * dim
    vec[0] = 1.0
    vec[axis] = nudge
    norm: float = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec]


class _AxisEncoder:
    """Encodes every query onto axis 0 so distance is fully controlled."""

    def encode(self, text: str) -> list[float]:
        return _unit(0)

    @property
    def dimension(self) -> int:
        return 384


def _real_vec_connection(db_path: Path) -> sqlite3.Connection:
    """Open a DB backed by the REAL sqlite-vec extension.

    ``tests/conftest.py`` replaces the ``sqlite_vec`` module with a no-op so the
    rest of the suite can run without the C extension. The vec0 pre-filter is
    exactly what these tests exist to exercise, so load the real package by
    bypassing that stub; skip when it is not installed.
    """
    spec = importlib.machinery.PathFinder.find_spec("sqlite_vec")
    if spec is None or spec.loader is None:
        pytest.skip("real sqlite-vec is not installed")
    real_vec = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real_vec)
    conn: sqlite3.Connection = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.enable_load_extension(True)
        real_vec.load(conn)
        conn.enable_load_extension(False)
    except (AttributeError, sqlite3.Error):
        conn.close()
        pytest.skip("sqlite extension loading is unavailable")
    initialize_db(conn, embedding_dim=384)
    return conn


_TAG_CASES: list[tuple[str, str]] = [
    ("w%nted", "wanted"),
    ("w_nted", "wanted"),
    ("WaNtEd", "wanted"),
    ("é", "decoy"),
    ("😀", "decoy"),
    ('a"b', "decoy"),
    ("a\\b", "decoy"),
]


class TestVecTagPrefilterIsExact:
    @pytest.mark.parametrize("top_k", [1, 2, 3])
    @pytest.mark.parametrize(("tag", "decoy_tag"), _TAG_CASES)
    def test_target_below_pool_cap_is_returned(
        self,
        tmp_path: Path,
        tag: str,
        decoy_tag: str,
        top_k: int,
    ) -> None:
        """Guards the exact ``json_each`` pre-filter in ``search_vec``.

        Fails on the LIKE predicate: wildcard / case decoys fill the pool
        (``w%nted``, ``w_nted``, ``WaNtEd`` vs decoys tagged ``wanted``) and
        JSON-escaped tags (accented, emoji, quote, backslash) never match at all.
        Only vector search can reach the target here: no lexical overlap with
        the query and importance below the always-load threshold.
        """
        conn: sqlite3.Connection = _real_vec_connection(tmp_path / "vec.db")
        try:
            cap: int = top_k * _CANDIDATE_POOL_MULTIPLIER
            for i in range(cap + 2):
                decoy = _make_record(f"decoy-{i}", tags=[decoy_tag])
                decoy.content = f"filler{i} zzdecoy"
                insert_memory(conn, decoy, _near(1, 0.01 * (i + 1)))
            target = _make_record("target", tags=[tag])
            target.content = "unrelated qqtarget"
            insert_memory(conn, target, _unit(5))
            conn.commit()

            results = search_memories(
                conn,
                _AxisEncoder(),  # type: ignore[arg-type]
                query="lookup phrase",
                top_k=top_k,
                tags=[tag],
                update_access_on_retrieve=False,
            )
        finally:
            conn.close()

        ids: list[str] = [r.memory.id for r in results]
        assert ids == ["target"]

    def test_similar_tag_decoy_never_returned(self, tmp_path: Path) -> None:
        """A decoy tagged ``wanted`` must not satisfy ``tags=['w_nted']``."""
        conn: sqlite3.Connection = _real_vec_connection(tmp_path / "vec2.db")
        try:
            for i in range(4):
                decoy = _make_record(f"decoy-{i}", tags=["wanted"])
                decoy.content = f"filler{i} zzdecoy"
                insert_memory(conn, decoy, _near(1, 0.01 * (i + 1)))
            target = _make_record("target", tags=["w_nted"])
            target.content = "unrelated qqtarget"
            insert_memory(conn, target, _unit(5))
            conn.commit()

            results = search_memories(
                conn,
                _AxisEncoder(),  # type: ignore[arg-type]
                query="lookup phrase",
                top_k=3,
                tags=["w_nted"],
                update_access_on_retrieve=False,
            )
        finally:
            conn.close()

        ids: list[str] = [r.memory.id for r in results]
        assert "decoy-0" not in ids
        assert not any(i.startswith("decoy-") for i in ids)
        assert ids == ["target"]


class TestVecMultiTagPrefilterIsAnd:
    @pytest.mark.parametrize("top_k", [1, 2, 3])
    def test_partial_match_decoys_do_not_exhaust_pool(
        self, tmp_path: Path, top_k: int
    ) -> None:
        """Required tags are AND-chained inside search_vec's own SQL.

        Closer decoys carry only ONE of the two required tags. If the
        per-tag predicates were OR-chained they would fill the candidate
        cap and evict the only row carrying BOTH.
        """
        conn: sqlite3.Connection = _real_vec_connection(tmp_path / "and.db")
        try:
            cap: int = top_k * _CANDIDATE_POOL_MULTIPLIER
            # Each single-tag population alone exceeds the cap, so dropping
            # any required tag (not just OR-chaining) exhausts the pool.
            n: int = 2 * (cap + 2)
            for i in range(2 * n):
                only: str = "alpha" if i % 2 else "beta"
                partial = _make_record(f"partial-{i}", tags=[only])
                partial.content = f"filler{i} zzpartial"
                insert_memory(conn, partial, _near(1 + i % 2, 0.001 * (i + 1)))
            target = _make_record("target", tags=["alpha", "beta"])
            target.content = "unrelated qqtarget"
            insert_memory(conn, target, _unit(5))
            conn.commit()

            results = search_memories(
                conn,
                _AxisEncoder(),  # type: ignore[arg-type]
                query="lookup phrase",
                top_k=top_k,
                tags=["alpha", "beta"],
                update_access_on_retrieve=False,
            )
        finally:
            conn.close()

        assert [r.memory.id for r in results] == ["target"]


class TestVecTagPrefilterIsSubset:
    @pytest.mark.parametrize("top_k", [1, 2, 3])
    @pytest.mark.parametrize(("stored", "required"), [
        (["alpha", "beta", "extra"], ["alpha", "beta"]),
        (["beta", "alpha"], ["alpha", "beta"]),
        (["extra", "alpha", "beta"], ["alpha", "beta"]),
        (["alpha", "beta"], ["beta", "alpha"]),
    ])
    def test_superset_tagged_target_is_returned(
        self, tmp_path: Path, top_k: int, stored: list[str], required: list[str]
    ) -> None:
        """Required tags are an order-insensitive SUBSET test, not exact-set or positional.

        The target carries [alpha, beta, extra] and the query requires
        [alpha, beta] (a strict subset). Closer decoys are tagged only [decoy], so just the target satisfies
        the filter and only the vector path can reach it (no lexical overlap,
        importance below always-load). An exact-set predicate (array length
        equal to the number of required tags) would exclude it.
        """
        conn: sqlite3.Connection = _real_vec_connection(tmp_path / "subset.db")
        try:
            cap: int = top_k * _CANDIDATE_POOL_MULTIPLIER
            for i in range(cap + 2):
                decoy = _make_record(f"decoy-{i}", tags=["decoy"])
                decoy.content = f"filler{i} zzdecoy"
                insert_memory(conn, decoy, _near(1, 0.01 * (i + 1)))
            target = _make_record("target", tags=stored)
            target.content = "unrelated qqtarget"
            insert_memory(conn, target, _unit(5))
            conn.commit()

            results = search_memories(
                conn,
                _AxisEncoder(),  # type: ignore[arg-type]
                query="lookup phrase",
                top_k=top_k,
                tags=required,
                update_access_on_retrieve=False,
            )
        finally:
            conn.close()

        assert [r.memory.id for r in results] == ["target"]
