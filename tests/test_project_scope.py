"""Tests for project scope enforcement in search_memories (issue #9)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from claude_memory.db.queries import (
    MemoryRecord,
    _register_read_scope_function,
    _register_recall_scope_function,
    get_always_load,
    insert_memory,
    search_fts,
)
from claude_memory.retrieval.search import (
    _apply_project_scope_filter,
    _derive_project_id,
    recall_session_memories,
    search_memories,
)
from claude_memory.scope import is_read_scope_compatible, is_recall_scope_compatible

from tests.conftest import MockEncoder


def _now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_record(
    id: str,
    tags: list[str],
    project_dir: str | None = None,
    importance: float = 5.0,
) -> MemoryRecord:
    return MemoryRecord(
        id=id,
        content=f"Content {id}",
        summary=None,
        type="lesson",
        tags=tags,
        created_at=_now_str(),
        updated_at=_now_str(),
        last_accessed=_now_str(),
        access_count=0,
        importance=importance,
        tier="hot",
        project_dir=project_dir,
        source_session=None,
        supersedes=None,
        consolidated_from=[],
        metadata={},
    )


class TestDeriveProjectId:
    def test_unix_style_path(self):
        assert _derive_project_id("/projects/Example.App") == "example-app"

    def test_windows_backslash(self):
        assert _derive_project_id("C:\\code\\Beta") == "beta"

    def test_windows_forward_slash(self):
        assert _derive_project_id("C:/code/Beta") == "beta"

    def test_trailing_slash_ignored(self):
        assert _derive_project_id("/projects/Beta/") == "beta"

    def test_lowercase(self):
        assert _derive_project_id("/projects/SideProject") == "sideproject"

    def test_none_returns_none(self):
        assert _derive_project_id(None) is None

    def test_empty_string_returns_none(self):
        assert _derive_project_id("") is None

    def test_underscore_becomes_hyphen(self):
        assert _derive_project_id("/projects/my_project") == "my-project"

    def test_project_root_has_no_tag_identifier(self):
        assert _derive_project_id("C:\\") is None
        assert _derive_project_id("/") is None


class TestApplyProjectScopeFilter:
    def _records(self) -> dict[str, MemoryRecord]:
        return {
            # Belongs to example-app — should pass for Example.App caller
            "m1": _make_record("m1", ["project:example-app", "kind:tripwire"]),
            # Belongs to beta — should be filtered for Example.App caller
            "m2": _make_record("m2", ["project:beta", "scope:cookbook"]),
            # Cross-project — should ALWAYS pass regardless of caller
            "m3": _make_record("m3", ["project:beta", "scope:cross-project"]),
            # No project tag — should always pass
            "m4": _make_record("m4", ["kind:lesson"]),
            # Multiple project tags with cross-project — passes
            "m5": _make_record("m5", ["project:example-app", "scope:cross-project"]),
            # No tags at all — should always pass
            "m6": _make_record("m6", []),
        }

    def test_no_project_dir_returns_all(self):
        records = self._records()
        result = _apply_project_scope_filter(records, project_dir=None)
        assert set(result.keys()) == {"m1", "m2", "m3", "m4", "m5", "m6"}

    def test_filters_wrong_project_tag(self):
        records = self._records()
        result = _apply_project_scope_filter(records, project_dir="/projects/Example.App")
        assert "m2" not in result  # project:beta, no cross-project

    def test_keeps_matching_project_tag(self):
        records = self._records()
        result = _apply_project_scope_filter(records, project_dir="/projects/Example.App")
        assert "m1" in result

    def test_keeps_cross_project_despite_wrong_project(self):
        records = self._records()
        result = _apply_project_scope_filter(records, project_dir="/projects/Example.App")
        assert "m3" in result  # project:beta but scope:cross-project

    def test_keeps_no_project_tag_records(self):
        records = self._records()
        result = _apply_project_scope_filter(records, project_dir="/projects/Example.App")
        assert "m4" in result
        assert "m6" in result

    def test_beta_caller_filters_example_memories(self):
        records = self._records()
        result = _apply_project_scope_filter(records, project_dir="/projects/Beta")
        # m1 is project:example-app (no cross-project) → filtered
        assert "m1" not in result
        # m2 is project:beta → passes
        assert "m2" in result
        # m3 is project:beta + scope:cross-project → passes
        assert "m3" in result

    def test_explicit_foreign_project_dir_is_filtered_without_project_tags(self):
        records = {
            "foreign": _make_record(
                "foreign",
                ["kind:lesson"],
                project_dir="Z:\\Personal\\PackShipApp",
            ),
        }

        result = _apply_project_scope_filter(
            records,
            project_dir="Z:\\Personal\\w31rd.com",
        )

        assert "foreign" not in result

    def test_cross_project_tag_cannot_override_explicit_foreign_project_dir(self):
        records = {
            "foreign": _make_record(
                "foreign",
                ["project:packshipapp", "scope:cross-project"],
                project_dir="Z:\\Personal\\PackShipApp",
            ),
        }

        result = _apply_project_scope_filter(
            records,
            project_dir="Z:\\Personal\\w31rd.com",
        )

        assert "foreign" not in result

    def test_equivalent_windows_project_paths_are_kept(self):
        records = {
            "same": _make_record(
                "same",
                ["project:stale-tag"],
                project_dir="z:/personal/W31RD.COM/",
            ),
        }

        result = _apply_project_scope_filter(
            records,
            project_dir="Z:\\Personal\\w31rd.com",
        )

        assert "same" in result

    def test_posix_project_paths_remain_case_sensitive(self):
        records = {
            "foreign": _make_record(
                "foreign",
                ["kind:lesson"],
                project_dir="/Projects/Example",
            ),
        }

        result = _apply_project_scope_filter(
            records,
            project_dir="/projects/example",
        )

        assert "foreign" not in result

    def test_posix_trailing_whitespace_remains_part_of_scope(self):
        records = {
            "foreign": _make_record(
                "foreign",
                ["kind:lesson"],
                project_dir="/projects/example ",
            ),
        }

        result = _apply_project_scope_filter(
            records,
            project_dir="/projects/example",
        )

        assert "foreign" not in result

    def test_windows_root_scope_still_enforces_explicit_project_dir(self):
        records = {
            "foreign": _make_record(
                "foreign",
                ["kind:lesson"],
                project_dir="D:\\",
            ),
        }

        result = _apply_project_scope_filter(records, project_dir="C:\\")

        assert "foreign" not in result

    def test_posix_root_scope_still_enforces_explicit_project_dir(self):
        records = {
            "foreign": _make_record(
                "foreign",
                ["kind:lesson"],
                project_dir="/projects/example",
            ),
        }

        result = _apply_project_scope_filter(records, project_dir="/")

        assert "foreign" not in result


def _insert_record(
    conn: sqlite3.Connection,
    encoder: MockEncoder,
    record: MemoryRecord,
) -> None:
    insert_memory(conn, record, encoder.encode(record.content))


class TestProjectScopeIntegration:
    def test_enforced_search_excludes_explicit_foreign_vector_match(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        foreign = _make_record(
            "foreign",
            ["project:packshipapp", "scope:cross-project"],
            project_dir="Z:\\Personal\\PackShipApp",
        )
        _insert_record(db_conn, mock_encoder, foreign)

        with (
            patch(
                "claude_memory.retrieval.search.search_vec",
                return_value=[("foreign", 0.01)],
            ),
            patch("claude_memory.retrieval.search.search_fts", return_value=[]),
            patch("claude_memory.retrieval.search.get_always_load", return_value=[]),
        ):
            results = search_memories(
                db_conn,
                mock_encoder,
                query="Codex Johnny-Five MCP connectivity",
                project_dir="Z:\\Personal\\w31rd.com",
                enforce_project_scope=True,
                update_access_on_retrieve=False,
            )

        assert [result.memory.id for result in results] == []

    def test_unenforced_diagnostic_search_can_return_explicit_foreign_scope(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        foreign = _make_record(
            "foreign",
            ["project:packshipapp"],
            project_dir="Z:\\Personal\\PackShipApp",
        )
        _insert_record(db_conn, mock_encoder, foreign)

        with (
            patch(
                "claude_memory.retrieval.search.search_vec",
                return_value=[("foreign", 0.01)],
            ),
            patch("claude_memory.retrieval.search.search_fts", return_value=[]),
            patch("claude_memory.retrieval.search.get_always_load", return_value=[]),
        ):
            results = search_memories(
                db_conn,
                mock_encoder,
                query="cross-project diagnostic",
                project_dir="Z:\\Personal\\w31rd.com",
                enforce_project_scope=False,
                update_access_on_retrieve=False,
            )

        assert [result.memory.id for result in results] == ["foreign"]

    def test_session_recall_blocks_explicit_foreign_scope_but_keeps_legacy_tags(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        foreign = _make_record(
            "foreign",
            ["scope:cross-project"],
            project_dir="Z:\\Personal\\PackShipApp",
        )
        legacy = _make_record(
            "legacy",
            ["project:packshipapp"],
            project_dir=None,
        )
        _insert_record(db_conn, mock_encoder, foreign)
        _insert_record(db_conn, mock_encoder, legacy)

        with (
            patch(
                "claude_memory.retrieval.search.search_vec",
                return_value=[],
            ),
            patch("claude_memory.retrieval.search.search_fts", return_value=[]),
            patch(
                "claude_memory.retrieval.search.get_always_load",
                return_value=["foreign", "legacy"],
            ),
        ):
            results = recall_session_memories(
                db_conn,
                mock_encoder,
                project_dir="Z:\\Personal\\w31rd.com",
                initial_context="memory isolation",
            )

        result_ids = [result.memory.id for result in results]
        assert "foreign" not in result_ids
        assert "legacy" in result_ids


class TestCanonicalScopeCandidateAcquisition:
    def _insert_windows_scope_records(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        matching = _make_record(
            "matching",
            [],
            project_dir="z:/personal/W31RD.COM/",
            importance=9.0,
        )
        foreign = _make_record(
            "foreign",
            [],
            project_dir="Z:\\Personal\\Neighborly",
            importance=10.0,
        )
        _insert_record(db_conn, mock_encoder, matching)
        _insert_record(db_conn, mock_encoder, foreign)

    def test_fts_acquisition_uses_canonical_windows_scope(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        self._insert_windows_scope_records(db_conn, mock_encoder)

        results = search_fts(
            db_conn,
            "Content",
            project_dir="Z:\\Personal\\w31rd.com",
        )

        assert [memory_id for memory_id, _ in results] == ["matching"]

    def test_always_load_acquisition_uses_canonical_windows_scope(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        self._insert_windows_scope_records(db_conn, mock_encoder)

        result_ids = get_always_load(
            db_conn,
            project_dir="Z:\\Personal\\w31rd.com",
        )

        assert result_ids == ["matching"]

    def test_search_uses_canonical_fts_candidates(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        self._insert_windows_scope_records(db_conn, mock_encoder)

        with patch("claude_memory.retrieval.search.search_vec", return_value=[]):
            results = search_memories(
                db_conn,
                mock_encoder,
                query="Content",
                project_dir="Z:\\Personal\\w31rd.com",
                update_access_on_retrieve=False,
            )

        assert [result.memory.id for result in results] == ["matching"]

    def test_empty_context_recall_uses_canonical_always_load_candidates(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        self._insert_windows_scope_records(db_conn, mock_encoder)

        results = recall_session_memories(
            db_conn,
            mock_encoder,
            project_dir="Z:\\Personal\\w31rd.com",
            initial_context="",
        )

        assert [result.memory.id for result in results] == ["matching"]

    @pytest.mark.parametrize("requested_project_dir", [None, "", "   "])
    def test_global_semantic_recall_excludes_explicit_project_memories(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
        requested_project_dir: str | None,
    ) -> None:
        global_record = _make_record("global", [], project_dir=None)
        foreign = _make_record(
            "foreign",
            [],
            project_dir="Z:\\Personal\\Neighborly",
        )
        _insert_record(db_conn, mock_encoder, global_record)
        _insert_record(db_conn, mock_encoder, foreign)

        with patch("claude_memory.retrieval.search.search_vec", return_value=[]):
            results = recall_session_memories(
                db_conn,
                mock_encoder,
                project_dir=requested_project_dir,
                initial_context="Content",
            )

        assert [result.memory.id for result in results] == ["global"]

    def test_global_always_load_includes_legacy_blank_scope(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        blank = _make_record(
            "blank",
            [],
            project_dir="   ",
            importance=9.0,
        )
        foreign = _make_record(
            "foreign",
            [],
            project_dir="Z:\\Personal\\Neighborly",
            importance=10.0,
        )
        _insert_record(db_conn, mock_encoder, blank)
        _insert_record(db_conn, mock_encoder, foreign)

        result_ids = get_always_load(db_conn, project_dir=None)

        assert result_ids == ["blank"]

    def test_global_semantic_recall_is_not_starved_by_scoped_fts_matches(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        for index in range(4):
            foreign = _make_record(
                f"foreign-{index}",
                [],
                project_dir="Z:\\Personal\\Neighborly",
            )
            foreign.content = "starvation marker starvation marker"
            _insert_record(db_conn, mock_encoder, foreign)

        global_record = _make_record("global", [], project_dir=None)
        global_record.content = "starvation marker"
        _insert_record(db_conn, mock_encoder, global_record)

        with patch("claude_memory.retrieval.search.search_vec", return_value=[]):
            results = recall_session_memories(
                db_conn,
                mock_encoder,
                project_dir=None,
                initial_context="starvation marker",
                top_k=1,
            )

        assert [result.memory.id for result in results] == ["global"]


class TestVectorScopeAppliedBeforeLimit:
    """Defect: search_vec's LIMIT used to run before any project-scope check.

    Unlike search_fts (which has always applied its scope predicate inside
    the query, before LIMIT), search_vec used to have no scope awareness at
    all — scoping happened only afterwards, in search.py, once the pool was
    already truncated. A flood of closer out-of-scope vectors could fill the
    entire bounded pool on their own, and every one of them would then be
    discarded by the later Python-side scope filter — leaving zero semantic
    compensation for an in-scope match that was never even fetched.

    The fix pushes the scope predicate into search_vec's own SQL, before its
    LIMIT (the same shape search_fts already used). These tests fake
    search_vec with a stand-in that faithfully reproduces that scope-then-
    limit contract and verify search.py now supplies the project_dir /
    recall_scope it needs — proving the wiring, not the SQL text itself
    (this suite runs without the compiled sqlite-vec extension, so the real
    vec0 query can't be executed here; see the session report).
    """

    def test_search_memories_survives_a_foreign_vector_flood(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """45 closer foreign-project vectors must not evict the in-scope hit."""
        matching = _make_record(
            "matching", [], project_dir="/projects/alpha", importance=5.0,
        )
        _insert_record(db_conn, mock_encoder, matching)
        project_dirs: dict[str, str | None] = {"matching": matching.project_dir}
        for i in range(45):
            foreign_id = f"foreign-{i}"
            foreign = _make_record(
                foreign_id, [], project_dir="/projects/beta", importance=5.0,
            )
            _insert_record(db_conn, mock_encoder, foreign)
            project_dirs[foreign_id] = foreign.project_dir

        def fake_search_vec(
            conn: sqlite3.Connection,
            embedding: list[float],
            top_k: int = 50,
            project_dir: str | None = None,
            recall_scope: bool = False,
            required_tags: list[str] | None = None,
        ) -> list[tuple[str, float]]:
            # Every foreign vector ranks closer than "matching" — the shape
            # that starves a scope-blind pool. Branches exactly like the
            # real search_vec SQL so this is a faithful stand-in.
            ranked: list[tuple[str, float]] = [
                (f"foreign-{i}", 0.01 + i * 0.001) for i in range(45)
            ]
            ranked.append(("matching", 0.90))
            if recall_scope:
                compatible = [
                    (mid, dist) for mid, dist in ranked
                    if is_recall_scope_compatible(project_dirs[mid], project_dir)
                ]
            elif project_dir is not None:
                compatible = [
                    (mid, dist) for mid, dist in ranked
                    if is_read_scope_compatible(project_dirs[mid], project_dir)
                ]
            else:
                compatible = ranked
            return compatible[:top_k]

        with (
            patch(
                "claude_memory.retrieval.search.search_vec",
                side_effect=fake_search_vec,
            ) as vec_spy,
            patch("claude_memory.retrieval.search.search_fts", return_value=[]),
            patch("claude_memory.retrieval.search.get_always_load", return_value=[]),
        ):
            results = search_memories(
                db_conn,
                mock_encoder,
                query="Codex Johnny-Five MCP connectivity",
                project_dir="/projects/alpha",
                update_access_on_retrieve=False,
            )

        assert [result.memory.id for result in results] == ["matching"]
        assert vec_spy.call_args.kwargs["project_dir"] == "/projects/alpha"

    def test_recall_session_memories_survives_a_foreign_vector_flood(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """Vector-pool mirror of
        test_global_semantic_recall_is_not_starved_by_scoped_fts_matches —
        same starvation shape, but through the vector pool instead of FTS.
        """
        global_record = _make_record("global", [], project_dir=None)
        _insert_record(db_conn, mock_encoder, global_record)
        project_dirs: dict[str, str | None] = {"global": None}
        for index in range(4):
            foreign_id = f"foreign-{index}"
            foreign = _make_record(
                foreign_id, [], project_dir="/projects/beta",
            )
            _insert_record(db_conn, mock_encoder, foreign)
            project_dirs[foreign_id] = foreign.project_dir

        def fake_search_vec(
            conn: sqlite3.Connection,
            embedding: list[float],
            top_k: int = 50,
            project_dir: str | None = None,
            recall_scope: bool = False,
            required_tags: list[str] | None = None,
        ) -> list[tuple[str, float]]:
            ranked: list[tuple[str, float]] = [
                (f"foreign-{i}", 0.01 + i * 0.001) for i in range(4)
            ]
            ranked.append(("global", 0.90))
            if recall_scope:
                compatible = [
                    (mid, dist) for mid, dist in ranked
                    if is_recall_scope_compatible(project_dirs[mid], project_dir)
                ]
            elif project_dir is not None:
                compatible = [
                    (mid, dist) for mid, dist in ranked
                    if is_read_scope_compatible(project_dirs[mid], project_dir)
                ]
            else:
                compatible = ranked
            return compatible[:top_k]

        with (
            patch(
                "claude_memory.retrieval.search.search_vec",
                side_effect=fake_search_vec,
            ) as vec_spy,
            patch("claude_memory.retrieval.search.search_fts", return_value=[]),
        ):
            results = recall_session_memories(
                db_conn,
                mock_encoder,
                project_dir=None,
                initial_context="starvation marker",
                top_k=1,
            )

        assert [result.memory.id for result in results] == ["global"]
        assert vec_spy.call_args.kwargs["recall_scope"] is True


class TestScopePredicateSqlFunctionMatchesPurePython:
    """Disclosure + proof for the scope-predicate choice in search_vec's fix.

    search_vec's new id-eligibility pre-filter (the vec0-LIMIT-before-scope
    fix) calls the SQL functions ``j5_read_scope_compatible`` /
    ``j5_recall_scope_compatible`` — the same functions search_fts and
    get_always_load's reservation split already used; nothing new was
    introduced for this fix, and no separate SQL expression exists anywhere
    in this codebase for it to have diverged from. Both registered
    functions (see ``_register_read_scope_function`` /
    ``_register_recall_scope_function`` in ``claude_memory.db.queries``)
    are thin ``conn.create_function`` wrappers directly around
    ``is_read_scope_compatible`` / ``is_recall_scope_compatible``
    (``claude_memory.scope``) — so there is no independent algorithm to
    test for equivalence, only the SQLite marshaling boundary itself
    (``NULL`` <-> ``None``, ``TEXT`` <-> ``str``) that a bug could hide in.
    This asserts the SQL-callable path agrees with calling the Python
    function directly across the matrix that boundary could plausibly
    disturb: project match, foreign, NULL/global, empty string,
    trailing-separator, and case variants.
    """

    _MATRIX: list[tuple[str | None, str | None]] = [
        ("/projects/alpha", "/projects/alpha"),  # exact match
        ("/projects/alpha", "/projects/beta"),  # foreign
        (None, "/projects/alpha"),  # record global, scoped requester
        ("/projects/alpha", None),  # record scoped, unscoped requester
        (None, None),  # both global
        ("", "/projects/alpha"),  # blank-string record treated as global
        ("/projects/alpha", ""),  # blank-string requester treated as global
        ("   ", "/projects/alpha"),  # whitespace-only record treated as global
        ("/projects/alpha/", "/projects/alpha"),  # trailing separator, record
        ("/projects/alpha", "/projects/alpha/"),  # trailing separator, requester
        ("C:\\FakeProjects\\Alpha", "C:\\FakeProjects\\ALPHA"),  # Windows case-insensitive
        ("C:\\FakeProjects\\Alpha", "c:/fakeprojects/alpha"),  # Windows slash + case variant
        ("/Projects/Alpha", "/projects/alpha"),  # POSIX stays case-sensitive -> mismatch
    ]

    @pytest.mark.parametrize(("record_scope", "requested_scope"), _MATRIX)
    def test_read_predicate_sql_matches_python(
        self,
        db_conn: sqlite3.Connection,
        record_scope: str | None,
        requested_scope: str | None,
    ) -> None:
        _register_read_scope_function(db_conn)
        sql_result = db_conn.execute(
            "SELECT j5_read_scope_compatible(?, ?) AS r",
            (record_scope, requested_scope),
        ).fetchone()["r"]
        python_result: bool = is_read_scope_compatible(record_scope, requested_scope)
        assert bool(sql_result) == python_result

    @pytest.mark.parametrize(("record_scope", "requested_scope"), _MATRIX)
    def test_recall_predicate_sql_matches_python(
        self,
        db_conn: sqlite3.Connection,
        record_scope: str | None,
        requested_scope: str | None,
    ) -> None:
        _register_recall_scope_function(db_conn)
        sql_result = db_conn.execute(
            "SELECT j5_recall_scope_compatible(?, ?) AS r",
            (record_scope, requested_scope),
        ).fetchone()["r"]
        python_result: bool = is_recall_scope_compatible(record_scope, requested_scope)
        assert bool(sql_result) == python_result
