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
        assert _derive_project_id("Z:/Personal/w31rd.com") == "w31rd-com"

    def test_windows_backslash(self):
        assert _derive_project_id("Z:\\Personal\\Hac") == "hac"

    def test_trailing_slash_ignored(self):
        assert _derive_project_id("Z:/Personal/Hac/") == "hac"

    def test_lowercase(self):
        assert _derive_project_id("Z:/Personal/johnnyFive") == "johnnyfive"

    def test_none_returns_none(self):
        assert _derive_project_id(None) is None

    def test_empty_string_returns_none(self):
        assert _derive_project_id("") is None

    def test_underscore_becomes_hyphen(self):
        assert _derive_project_id("Z:/Personal/my_project") == "my-project"


class TestApplyProjectScopeFilter:
    def _records(self) -> dict[str, MemoryRecord]:
        return {
            # Belongs to w31rd-com — should pass for w31rd.com caller
            "m1": _make_record("m1", ["project:w31rd-com", "kind:tripwire"]),
            # Belongs to hac — should be filtered for w31rd.com caller
            "m2": _make_record("m2", ["project:hac", "scope:cookbook"]),
            # Cross-project — should ALWAYS pass regardless of caller
            "m3": _make_record("m3", ["project:hac", "scope:cross-project"]),
            # No project tag — should always pass
            "m4": _make_record("m4", ["kind:lesson"]),
            # Multiple project tags with cross-project — passes
            "m5": _make_record("m5", ["project:w31rd-com", "scope:cross-project"]),
            # No tags at all — should always pass
            "m6": _make_record("m6", []),
        }

    def test_no_project_dir_returns_all(self):
        records = self._records()
        result = _apply_project_scope_filter(records, project_dir=None)
        assert set(result.keys()) == {"m1", "m2", "m3", "m4", "m5", "m6"}

    def test_filters_wrong_project_tag(self):
        records = self._records()
        result = _apply_project_scope_filter(records, project_dir="Z:/Personal/w31rd.com")
        assert "m2" not in result  # project:hac, no cross-project

    def test_keeps_matching_project_tag(self):
        records = self._records()
        result = _apply_project_scope_filter(records, project_dir="Z:/Personal/w31rd.com")
        assert "m1" in result

    def test_keeps_cross_project_despite_wrong_project(self):
        records = self._records()
        result = _apply_project_scope_filter(records, project_dir="Z:/Personal/w31rd.com")
        assert "m3" in result  # project:hac but scope:cross-project

    def test_keeps_no_project_tag_records(self):
        records = self._records()
        result = _apply_project_scope_filter(records, project_dir="Z:/Personal/w31rd.com")
        assert "m4" in result
        assert "m6" in result

    def test_hac_caller_filters_w31rd_memories(self):
        records = self._records()
        result = _apply_project_scope_filter(records, project_dir="Z:/Personal/Hac")
        # m1 is project:w31rd-com (no cross-project) → filtered
        assert "m1" not in result
        # m2 is project:hac → passes
        assert "m2" in result
        # m3 is project:hac + scope:cross-project → passes
        assert "m3" in result
