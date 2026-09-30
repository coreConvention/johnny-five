"""``get_latest_session_state``: direct, ranking-independent resume lookup.

Every test here fails on origin/main, where the function does not exist.
Contract: scope (never global, never foreign), tag set, tier, exact-instant
ordering (offsets, microseconds, calendar edges) and read-only behaviour.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from claude_memory.db import queries

PROJECT: str = "/proj"
NOW: str = "2026-09-29T12:00:00+00:00"
T10: str = "2026-09-29T10:00:00+00:00"
T11: str = "2026-09-29T11:00:00+00:00"


def _lookup(conn: sqlite3.Connection, pd: str | None, **kw: object) -> str | None:
    # Attribute access so a missing function fails the test, not collection.
    row = queries.get_latest_session_state(conn, pd, **kw)  # type: ignore[attr-defined]
    return None if row is None else row["id"]


def _row(
    conn: sqlite3.Connection,
    id: str,
    created_at: str,
    *,
    tags: list[str] | None = None,
    project_dir: str | None = PROJECT,
    tier: str = "hot",
    importance: float = 5.0,
    access_count: int = 3,
) -> None:
    conn.execute(
        "INSERT INTO memories (id, content, type, tags, created_at, updated_at,"
        " last_accessed, access_count, importance, tier, project_dir)"
        " VALUES (?, ?, 'project', ?, ?, ?, ?, ?, ?, ?, ?)",
        (id, f"content of {id}", json.dumps(["session-state"] if tags is None else tags),
         created_at, created_at, NOW, access_count, importance, tier, project_dir),
    )
    # Commit: an open write transaction refuses create_function (issue #31).
    conn.commit()


class TestGuards:
    @pytest.mark.parametrize("blank", [None, "", "   ", "\t\n"])
    def test_blank_project_dir_returns_none(self, db_conn, blank) -> None:
        _row(db_conn, "a", T10)
        assert _lookup(db_conn, blank) is None

    def test_empty_tags_returns_none(self, db_conn) -> None:
        _row(db_conn, "a", T10)
        assert _lookup(db_conn, PROJECT, tags=()) is None

    def test_no_rows_returns_none(self, db_conn) -> None:
        assert _lookup(db_conn, PROJECT) is None


class TestTagsTierScope:
    @pytest.mark.parametrize("tag", ["session-state", "precompact", "kind:session-state"])
    def test_each_tag_alone_qualifies(self, db_conn, tag: str) -> None:
        _row(db_conn, "a", T10, tags=[tag])
        assert _lookup(db_conn, PROJECT) == "a"

    def test_row_without_any_tag_is_ignored(self, db_conn) -> None:
        _row(db_conn, "old", T10)
        _row(db_conn, "newer-untagged", T11, tags=["other", "x"])
        assert _lookup(db_conn, PROJECT) == "old"

    def test_archived_excluded(self, db_conn) -> None:
        _row(db_conn, "live", T10)
        _row(db_conn, "gone", T11, tier="archived")
        assert _lookup(db_conn, PROJECT) == "live"

    @pytest.mark.parametrize("global_value", [None, "", "  "])
    def test_global_row_never_returned_even_if_newest(self, db_conn, global_value) -> None:
        _row(db_conn, "scoped", T10)
        _row(db_conn, "global", T11, project_dir=global_value)
        assert _lookup(db_conn, PROJECT) == "scoped"

    def test_only_global_row_yields_none(self, db_conn) -> None:
        _row(db_conn, "global", T11, project_dir=None)
        assert _lookup(db_conn, PROJECT) is None

    def test_other_projects_row_never_returned(self, db_conn) -> None:
        _row(db_conn, "mine", T10)
        _row(db_conn, "theirs", T11, project_dir="/other")
        assert _lookup(db_conn, PROJECT) == "mine"
        assert _lookup(db_conn, "/nobody") is None

    def test_windows_canonical_match(self, db_conn) -> None:
        """Case, separator and trailing separator are all forgiven."""
        _row(db_conn, "win", T10, project_dir="Z:/proj/app")
        assert _lookup(db_conn, "z:\\proj\\app\\") == "win"

    def test_posix_stays_case_sensitive(self, db_conn) -> None:
        _row(db_conn, "posix", T10, project_dir="/proj")
        assert _lookup(db_conn, "/Proj") is None
        assert _lookup(db_conn, "/proj") == "posix"


class TestOrdering:
    def test_newest_by_instant_across_offsets(self, db_conn) -> None:
        """22:30+02:00 is 20:30Z: it loses to 21:30Z though it sorts later as text."""
        _row(db_conn, "offset", "2026-09-29T22:30:00+02:00")
        _row(db_conn, "utc", "2026-09-29T21:30:00+00:00")
        assert _lookup(db_conn, PROJECT) == "utc"

    def test_naive_timestamp_treated_as_utc(self, db_conn) -> None:
        _row(db_conn, "naive", "2026-09-29T21:00:00")
        _row(db_conn, "aware", "2026-09-29T21:30:00+00:00")
        assert _lookup(db_conn, PROJECT) == "aware"

    def test_equal_instants_higher_id_wins(self, db_conn) -> None:
        _row(db_conn, "a1", "2026-09-29T21:30:00+00:00")
        _row(db_conn, "a2", "2026-09-29T23:30:00+02:00")
        assert _lookup(db_conn, PROJECT) == "a2"

    def test_microsecond_precision_beats_id_tiebreak(self, db_conn) -> None:
        """A millisecond key ties these and would pick the older row by id."""
        _row(db_conn, "a", "2026-09-29T21:30:00.123400+00:00")
        _row(db_conn, "b", "2026-09-29T21:30:00.123100+00:00")
        assert _lookup(db_conn, PROJECT) == "a"

    def test_unparseable_created_at_sorts_last(self, db_conn) -> None:
        _row(db_conn, "z-garbage", "not a timestamp")
        _row(db_conn, "a-old", "2001-01-01T00:00:00+00:00")
        assert _lookup(db_conn, PROJECT) == "a-old"

    def test_unparseable_alone_still_returned(self, db_conn) -> None:
        _row(db_conn, "only", "garbage")
        assert _lookup(db_conn, PROJECT) == "only"

    def test_calendar_edge_values_do_not_abort_lookup(self, db_conn) -> None:
        """Converting these to UTC overflows; the key must use direct subtraction."""
        _row(db_conn, "early", "0001-01-01T00:00:00+00:01")
        _row(db_conn, "normal", T10)
        _row(db_conn, "late", "9999-12-31T23:59:59.999999-00:01")
        assert _lookup(db_conn, PROJECT) == "late"
        db_conn.execute("UPDATE memories SET tier = 'archived' WHERE id = 'late'")
        db_conn.commit()
        assert _lookup(db_conn, PROJECT) == "normal"


class TestReadOnly:
    def test_access_stats_unchanged_and_no_pending_write(self, db_conn) -> None:
        """Access stats feed the recency term this lookup exists to bypass."""
        _row(db_conn, "a", T10, access_count=7)
        _row(db_conn, "b", T11, access_count=2)
        sql: str = "SELECT id, access_count, last_accessed FROM memories ORDER BY id"
        before = [tuple(r) for r in db_conn.execute(sql)]
        assert _lookup(db_conn, PROJECT) == "b"
        assert [tuple(r) for r in db_conn.execute(sql)] == before
        assert db_conn.in_transaction is False


class TestNaiveAndUnfilteredChoice:
    def test_newest_naive_row_wins_over_older_aware_row(self, db_conn) -> None:
        """A naive value is UTC, not NULL (NULL would sort last)."""
        _row(db_conn, "aware", T10)
        _row(db_conn, "naive", "2026-09-29T11:30:00")
        assert _lookup(db_conn, PROJECT) == "naive"

    def test_naive_equal_instant_higher_id_wins(self, db_conn) -> None:
        _row(db_conn, "b-naive", "2026-09-29T21:30:00")
        _row(db_conn, "a-aware", "2026-09-29T21:30:00+00:00")
        assert _lookup(db_conn, PROJECT) == "b-naive"
        _row(db_conn, "c-aware", "2026-09-29T21:30:00+00:00")
        assert _lookup(db_conn, PROJECT) == "c-aware"

    def test_newer_low_importance_beats_older_high_importance(self, db_conn) -> None:
        """No importance floor: authored time decides."""
        _row(db_conn, "old-hot", T10, importance=10.0)
        _row(db_conn, "new-low", T11, importance=1.0)
        assert _lookup(db_conn, PROJECT) == "new-low"

    @pytest.mark.parametrize("tier", ["warm", "cold"])
    def test_newer_non_hot_tier_beats_older_hot(self, db_conn, tier: str) -> None:
        """Only 'archived' is excluded; tier otherwise plays no part."""
        _row(db_conn, "old-hot", T10, tier="hot", importance=10.0)
        _row(db_conn, "new", T11, tier=tier)
        assert _lookup(db_conn, PROJECT) == "new"
