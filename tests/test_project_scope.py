"""Tests for project scope enforcement in search_memories (issue #9)."""

from __future__ import annotations

from datetime import datetime, timezone

from claude_memory.db.queries import MemoryRecord
from claude_memory.retrieval.search import _apply_project_scope_filter, _derive_project_id


def _now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_record(id: str, tags: list[str], project_dir: str | None = None) -> MemoryRecord:
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
        importance=5.0,
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
