"""Tests for strict tags[] AND filter in search_memories (issues #8, #10)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from claude_memory.db.queries import MemoryRecord
from claude_memory.retrieval.search import _apply_tag_filter


def _now():
    return datetime.now(timezone.utc)


def _make_record(id: str, tags: list[str], project_dir: str | None = None) -> MemoryRecord:
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
        importance=5.0,
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
