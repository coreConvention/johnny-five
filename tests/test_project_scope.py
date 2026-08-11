"""Tests for project scope enforcement in search_memories (issue #9)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from claude_memory.db.queries import (
    MemoryRecord,
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
