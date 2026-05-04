"""Tests for summary_only flag on tool_memory_search (issue #7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from claude_memory.db.queries import MemoryRecord
from claude_memory.mcp.tools import _search_result_to_dict, _search_result_to_summary_dict
from claude_memory.retrieval.search import SearchResult


def _make_record(content: str = "long content here " * 20) -> MemoryRecord:
    now = datetime.now(timezone.utc)
    return MemoryRecord(
        id="test-id-1",
        content=content,
        summary=None,
        type="lesson",
        tags=["kind:tooling", "scope:cookbook"],
        created_at=(now - timedelta(days=1)).isoformat(),
        updated_at=now.isoformat(),
        last_accessed=now.isoformat(),
        access_count=3,
        importance=7.5,
        tier="hot",
        project_dir="Z:/Personal/w31rd.com",
        source_session=None,
        supersedes=None,
        consolidated_from=[],
        metadata={},
    )


def _make_result(content: str = "long content here " * 20) -> SearchResult:
    return SearchResult(
        memory=_make_record(content),
        score=0.85,
        semantic_score=0.80,
        recency_score=0.90,
        frequency_score=0.50,
        importance_score=0.75,
        lexical_score=0.60,
    )


class TestSearchResultToSummaryDict:
    def test_omits_full_content(self):
        result = _make_result()
        d = _search_result_to_summary_dict(result)
        assert "content" not in d

    def test_includes_preview_up_to_200_chars(self):
        long_content = "x" * 400
        result = _make_result(content=long_content)
        d = _search_result_to_summary_dict(result)
        assert "preview" in d
        assert len(d["preview"]) <= 204  # 200 chars + "..."

    def test_preview_ends_with_ellipsis_when_truncated(self):
        result = _make_result(content="x" * 400)
        d = _search_result_to_summary_dict(result)
        assert d["preview"].endswith("...")

    def test_short_content_no_ellipsis(self):
        result = _make_result(content="short")
        d = _search_result_to_summary_dict(result)
        assert d["preview"] == "short"
        assert not d["preview"].endswith("...")

    def test_includes_required_fields(self):
        result = _make_result()
        d = _search_result_to_summary_dict(result)
        for field in ("id", "type", "tags", "importance", "tier", "score",
                      "project_dir", "created_at", "updated_at", "access_count"):
            assert field in d, f"missing field: {field}"

    def test_omits_score_breakdown(self):
        result = _make_result()
        d = _search_result_to_summary_dict(result)
        for field in ("semantic_score", "recency_score", "frequency_score",
                      "importance_score", "lexical_score"):
            assert field not in d

    def test_exactly_200_chars_no_ellipsis(self):
        result = _make_result(content="x" * 200)
        d = _search_result_to_summary_dict(result)
        assert not d["preview"].endswith("...")
        assert len(d["preview"]) == 200

    def test_full_dict_still_has_content(self):
        result = _make_result(content="full content")
        d = _search_result_to_dict(result)
        assert d["content"] == "full content"
        assert "preview" not in d
