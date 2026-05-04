# memory_search Enhancements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement four `memory_search` improvements (GitHub issues #7–#10): `summary_only` flag, strict `tags[]` AND filter, project-scope enforcement, and correct `token_budget` ordering.

**Architecture:** All changes flow through two files — `src/claude_memory/retrieval/search.py` (pipeline logic) and `src/claude_memory/mcp/tools.py` (parameter surface + serialization). Tag filter (#8) runs after record lookup but before reranker, which automatically fixes `token_budget` ordering (#10). Project scope (#9) runs as a second post-lookup filter alongside tag filter. `summary_only` (#7) is a pure serialization concern in tools.py only.

**Tech Stack:** Python 3.11+, pytest, sqlite-vec, FTS5. No new dependencies.

---

## Issue Map

| Issue | Feature | Files changed |
|-------|---------|---------------|
| #7 | `summary_only: bool = False` — compact result shape without full content | `mcp/tools.py` |
| #8 | `tags: list[str] \| None = None` — strict AND pre-filter | `retrieval/search.py`, `mcp/tools.py` |
| #9 | Project scope enforcement — reject `project:<other>` memories unless `scope:cross-project` | `retrieval/search.py`, `mcp/tools.py` |
| #10 | `token_budget` ordering doc — resolved automatically when #8 tag filter runs before step 7 | `retrieval/search.py` (comment only) |

---

## Task 1: `summary_only` flag (Issue #7)

**Files:**
- Modify: `src/claude_memory/mcp/tools.py:71–90` (add `_search_result_to_summary_dict`)
- Modify: `src/claude_memory/mcp/tools.py:151–196` (add `summary_only` param to `tool_memory_search`)
- Test: `tests/test_summary_only.py` (new file)

### Step 1: Write the failing test

Create `tests/test_summary_only.py`:

```python
"""Tests for summary_only flag on tool_memory_search (issue #7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

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

    def test_full_dict_still_has_content(self):
        result = _make_result(content="full content")
        d = _search_result_to_dict(result)
        assert d["content"] == "full content"
        assert "preview" not in d
```

### Step 2: Run test to verify it fails

```bash
cd Z:/Personal/johnnyFive/.worktrees/feat-7-10-memory-search
python -m pytest tests/test_summary_only.py -v
```
Expected: `ImportError: cannot import name '_search_result_to_summary_dict'`

### Step 3: Add `_search_result_to_summary_dict` to tools.py

In `src/claude_memory/mcp/tools.py`, after line 90 (end of `_search_result_to_dict`), add:

```python
def _search_result_to_summary_dict(result: SearchResult) -> dict:
    """Compact serialization for two-pass retrieval workflows (issue #7).

    Omits full content — returns a ~200-char preview instead.
    Callers use memory_get(id) for full content of selected results.
    """
    tags = (
        result.memory.tags
        if isinstance(result.memory.tags, list)
        else json.loads(result.memory.tags or "[]")
    )
    content = result.memory.content or ""
    preview = content[:200] + "..." if len(content) > 200 else content
    return {
        "id": result.memory.id,
        "type": result.memory.type,
        "tags": tags,
        "importance": result.memory.importance,
        "tier": result.memory.tier,
        "score": round(result.score, 4),
        "preview": preview,
        "project_dir": result.memory.project_dir,
        "created_at": result.memory.created_at,
        "updated_at": result.memory.updated_at,
        "access_count": result.memory.access_count,
    }
```

### Step 4: Add `summary_only` param to `tool_memory_search`

Change the function signature and return statement in `src/claude_memory/mcp/tools.py` lines 151–196:

```python
async def tool_memory_search(
    query: str,
    project_dir: str | None = None,
    top_k: int | None = None,
    token_budget: int | None = None,
    summary_only: bool = False,
) -> dict:
    """Search memories using hybrid multi-signal retrieval.
    ...
    summary_only:
        When True, returns compact results (id, type, tags, importance, tier,
        score, 200-char preview, project_dir, timestamps, access_count) without
        the full ``content`` field.  Use for two-pass retrieval: first call with
        summary_only=True to find relevant IDs cheaply, then fetch full content
        with memory_get for selected results.
    """
    conn, encoder, settings = _get_deps()
    try:
        effective_top_k: int = top_k if top_k is not None else settings.top_k
        weights: ScoringWeights = _weights_from_settings(settings)

        results: list[SearchResult] = search_memories(
            conn=conn,
            encoder=encoder,
            query=query,
            project_dir=project_dir,
            weights=weights,
            top_k=effective_top_k,
            token_budget=token_budget,
        )
        conn.commit()
        serializer = _search_result_to_summary_dict if summary_only else _search_result_to_dict
        return {
            "results": [serializer(r) for r in results],
        }
    finally:
        conn.close()
```

### Step 5: Run tests to verify they pass

```bash
python -m pytest tests/test_summary_only.py -v
```
Expected: all 7 tests PASS.

### Step 6: Commit

```bash
git add src/claude_memory/mcp/tools.py tests/test_summary_only.py
git commit -m "feat: add summary_only flag to memory_search (issue #7)"
```

---

## Task 2: `tags[]` strict AND filter + `token_budget` ordering fix (Issues #8 + #10)

**Files:**
- Modify: `src/claude_memory/retrieval/search.py:182–276` (`search_memories` function)
- Modify: `src/claude_memory/mcp/tools.py:151–196` (`tool_memory_search` signature)
- Test: `tests/test_tag_filter.py` (new file)

### Step 1: Write the failing test

Create `tests/test_tag_filter.py`:

```python
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
```

### Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_tag_filter.py -v
```
Expected: `ImportError: cannot import name '_apply_tag_filter'`

### Step 3: Add `_apply_tag_filter` helper to search.py

In `src/claude_memory/retrieval/search.py`, after the `_update_access_stats` function (around line 108) and before the token-budget section, add:

```python
# ---------------------------------------------------------------------------
# Tag filter (issue #8)
#
# Strict AND filter — only memories possessing ALL required_tags survive.
# Runs after record lookup but before reranker so that token_budget (issue #10)
# and top_k limits apply to the already-filtered set, not the full candidate pool.
# ---------------------------------------------------------------------------

def _parse_tags(raw: list[str] | str | None) -> list[str]:
    """Return a normalised tag list regardless of how tags are stored."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    try:
        import json as _json
        parsed = _json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _apply_tag_filter(
    records: dict[str, MemoryRecord],
    required_tags: list[str] | None,
) -> dict[str, MemoryRecord]:
    """Return a subset of *records* where every required tag is present.

    When *required_tags* is None or empty, returns *records* unchanged (no-op).
    Tags on the record may be a Python list or a JSON-encoded string — both
    are handled.
    """
    if not required_tags:
        return records
    required = set(required_tags)
    return {
        mid: record
        for mid, record in records.items()
        if required.issubset(set(_parse_tags(record.tags)))
    }
```

### Step 4: Wire `_apply_tag_filter` into `search_memories`

In `search_memories()` (lines 182–276), insert step 4.5 after step 4 (lookup) and update the comment for step 7 to document #10 fix:

```python
    # 4. Lookup
    all_ids: list[str] = [c.memory_id for c in candidates]
    records: dict[str, MemoryRecord] = _lookup_records(conn, all_ids)

    # 4.5 Strict tag filter (issue #8 — runs before reranker so token_budget
    #     and top_k apply to the filtered set, fixing issue #10 ordering).
    if tags:
        records = _apply_tag_filter(records, tags)
        candidates = [c for c in candidates if c.memory_id in records]

    # 5. Rerank ...
```

Also update the function signature:

```python
def search_memories(
    conn: sqlite3.Connection,
    encoder: EmbeddingEncoder,
    query: str,
    project_dir: str | None = None,
    weights: ScoringWeights = ScoringWeights(),
    top_k: int = 15,
    update_access_on_retrieve: bool = True,
    token_budget: int | None = None,
    tags: list[str] | None = None,
) -> list[SearchResult]:
```

Add to the docstring Parameters section:
```
    tags:
        Optional list of tags that ALL returned memories must possess (strict
        AND filter).  Applied after record lookup but before reranker, so
        token_budget and top_k limits apply to the already-filtered candidate
        set (resolves issue #10 ordering).
```

### Step 5: Expose `tags` in `tool_memory_search`

In `src/claude_memory/mcp/tools.py`, update `tool_memory_search`:

```python
async def tool_memory_search(
    query: str,
    project_dir: str | None = None,
    top_k: int | None = None,
    token_budget: int | None = None,
    summary_only: bool = False,
    tags: list[str] | None = None,
) -> dict:
```

Pass `tags` through to `search_memories`:

```python
        results: list[SearchResult] = search_memories(
            conn=conn,
            encoder=encoder,
            query=query,
            project_dir=project_dir,
            weights=weights,
            top_k=effective_top_k,
            token_budget=token_budget,
            tags=tags,
        )
```

### Step 6: Run tests to verify they pass

```bash
python -m pytest tests/test_tag_filter.py -v
```
Expected: all 8 tests PASS.

### Step 7: Run full test suite to check no regressions

```bash
python -m pytest tests/ -v
```
Expected: all existing tests still PASS.

### Step 8: Commit

```bash
git add src/claude_memory/retrieval/search.py src/claude_memory/mcp/tools.py tests/test_tag_filter.py
git commit -m "feat: add tags[] strict AND filter to memory_search, fix token_budget ordering (issues #8, #10)"
```

---

## Task 3: Project scope enforcement (Issue #9)

**Files:**
- Modify: `src/claude_memory/retrieval/search.py` (add `_derive_project_id`, `_apply_project_scope_filter`, wire into `search_memories`)
- Modify: `src/claude_memory/mcp/tools.py` (expose `strict_project` param)
- Test: `tests/test_project_scope.py` (new file)

### Step 1: Write the failing test

Create `tests/test_project_scope.py`:

```python
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
```

### Step 2: Run test to verify it fails

```bash
python -m pytest tests/test_project_scope.py -v
```
Expected: `ImportError: cannot import name '_apply_project_scope_filter'`

### Step 3: Add project scope helpers to search.py

In `src/claude_memory/retrieval/search.py`, after `_apply_tag_filter`, add:

```python
# ---------------------------------------------------------------------------
# Project scope enforcement (issue #9)
#
# Memories with an explicit project:<X> tag that doesn't match the caller's
# project are filtered out, UNLESS the memory also has scope:cross-project.
# Memories with no project:<X> tag are always kept (they may be scoped via
# the project_dir DB field or be truly global).
# ---------------------------------------------------------------------------

import os as _os
import re as _re


def _derive_project_id(project_dir: str | None) -> str | None:
    """Normalize a project directory path to a tag-compatible project identifier.

    Examples
    --------
    ``Z:/Personal/w31rd.com`` → ``w31rd-com``
    ``Z:/Personal/Hac``       → ``hac``
    ``Z:/Personal/johnnyFive`` → ``johnnyfive``
    """
    if not project_dir:
        return None
    # Strip trailing slashes, take the last non-empty path component.
    name = _os.path.basename(project_dir.rstrip("/\\"))
    if not name:
        return None
    # Lowercase; replace dots and spaces with hyphens; strip fringe hyphens.
    name = _re.sub(r"[\s.]+", "-", name.lower()).strip("-")
    return name or None


def _apply_project_scope_filter(
    records: dict[str, MemoryRecord],
    project_dir: str | None,
) -> dict[str, MemoryRecord]:
    """Filter out memories that explicitly belong to a different project.

    A memory is removed when ALL of the following hold:
    - It carries at least one ``project:<X>`` tag.
    - None of those tags matches ``project:<caller_project_id>``.
    - It does NOT carry ``scope:cross-project``.

    Memories with no ``project:<X>`` tag are always kept.
    When *project_dir* is None (cross-project or anonymous call), no filtering
    is applied.
    """
    project_id = _derive_project_id(project_dir)
    if not project_id:
        return records

    caller_tag = f"project:{project_id}"
    filtered: dict[str, MemoryRecord] = {}
    for mid, record in records.items():
        tags = set(_parse_tags(record.tags))
        project_tags = {t for t in tags if t.startswith("project:")}
        if not project_tags:
            filtered[mid] = record  # no explicit project claim → keep
            continue
        if caller_tag in project_tags:
            filtered[mid] = record  # matches caller → keep
            continue
        if "scope:cross-project" in tags:
            filtered[mid] = record  # explicitly cross-project → keep
            continue
        # Has project:<other> tag and no cross-project scope → exclude
    return filtered
```

### Step 4: Wire `_apply_project_scope_filter` into `search_memories`

In `search_memories()`, add step 4.6 after step 4.5:

```python
    # 4.5 Strict tag filter (issue #8)
    if tags:
        records = _apply_tag_filter(records, tags)
        candidates = [c for c in candidates if c.memory_id in records]

    # 4.6 Project scope enforcement (issue #9) — reject project:<other> memories
    #     unless they carry scope:cross-project.
    if project_dir:
        records = _apply_project_scope_filter(records, project_dir)
        candidates = [c for c in candidates if c.memory_id in records]
```

Add to function signature and docstring:

```python
def search_memories(
    ...
    enforce_project_scope: bool = True,
) -> list[SearchResult]:
```

```
    enforce_project_scope:
        When True (default) and project_dir is provided, memories bearing a
        ``project:<other>`` tag that doesn't match the caller's project are
        excluded unless they carry ``scope:cross-project``.  Set to False for
        cross-project diagnostic queries.
```

And guard the step 4.6 block:

```python
    # 4.6 Project scope enforcement (issue #9)
    if project_dir and enforce_project_scope:
        records = _apply_project_scope_filter(records, project_dir)
        candidates = [c for c in candidates if c.memory_id in records]
```

### Step 5: Expose `enforce_project_scope` in `tool_memory_search`

In `src/claude_memory/mcp/tools.py`, add parameter:

```python
async def tool_memory_search(
    query: str,
    project_dir: str | None = None,
    top_k: int | None = None,
    token_budget: int | None = None,
    summary_only: bool = False,
    tags: list[str] | None = None,
    enforce_project_scope: bool = True,
) -> dict:
```

Pass through:

```python
        results: list[SearchResult] = search_memories(
            ...
            tags=tags,
            enforce_project_scope=enforce_project_scope,
        )
```

### Step 6: Run tests to verify they pass

```bash
python -m pytest tests/test_project_scope.py -v
```
Expected: all 11 tests PASS.

### Step 7: Run full test suite

```bash
python -m pytest tests/ -v
```
Expected: all tests PASS.

### Step 8: Commit

```bash
git add src/claude_memory/retrieval/search.py src/claude_memory/mcp/tools.py tests/test_project_scope.py
git commit -m "feat: enforce project scope in memory_search, reject project:<other> memories (issue #9)"
```

---

## Task 4: Final verification + PR

### Step 1: Run full test suite one more time

```bash
python -m pytest tests/ -v --tb=short
```
Expected: all tests PASS, no regressions.

### Step 2: Verify the new public API shape in the MCP __init__ exports

```bash
grep -n "tool_memory_search\|summary_only\|enforce_project_scope" \
  src/claude_memory/mcp/__init__.py \
  src/claude_memory/mcp/tools.py \
  src/claude_memory/retrieval/search.py
```
Confirm the new parameters appear in the tool definition that the MCP server registers.

### Step 3: Check the MCP server registers the updated schema

```bash
# Verify the MCP server sees the new params by checking tools registration
grep -n "summary_only\|enforce_project_scope\|tags" src/claude_memory/mcp/__init__.py
```
If the MCP schema is auto-derived from function signatures (FastMCP pattern), no additional changes needed. If schema is manually declared, add the new params.

### Step 4: Push branch and open PR

```bash
git push -u origin feat/7-10-memory-search
gh pr create \
  --title "feat: memory_search enhancements — summary_only, tags filter, project scope (#7-#10)" \
  --body "$(cat <<'EOF'
## Summary
- **#7** `summary_only` flag: compact result shape (id, type, tags, score, 200-char preview) for two-pass retrieval without blowing token budgets
- **#8** `tags[]` strict AND filter: pre-filter on exact tag match before ranker runs
- **#9** Project scope enforcement: reject `project:<other>` memories unless `scope:cross-project`
- **#10** `token_budget` ordering: resolved by #8 (tag filter now runs before token_budget cull); documented in code

## Test plan
- [ ] `tests/test_summary_only.py` — 7 tests for summary serialization
- [ ] `tests/test_tag_filter.py` — 8 tests for strict AND filter + JSON-tag handling
- [ ] `tests/test_project_scope.py` — 11 tests for project scope enforcement
- [ ] Full suite: `pytest tests/ -v` — no regressions

Closes #7, #8, #9, #10
EOF
)"
```

---

## Quick Reference: Files Changed

| File | Change type | What changed |
|------|------------|--------------|
| `src/claude_memory/mcp/tools.py` | Modified | Add `_search_result_to_summary_dict`, `summary_only` + `tags` + `enforce_project_scope` params |
| `src/claude_memory/retrieval/search.py` | Modified | Add `_parse_tags`, `_apply_tag_filter`, `_derive_project_id`, `_apply_project_scope_filter`; wire into `search_memories` as steps 4.5 and 4.6 |
| `tests/test_summary_only.py` | New | 7 tests for `summary_only` output shape |
| `tests/test_tag_filter.py` | New | 8 tests for strict AND tag filter |
| `tests/test_project_scope.py` | New | 11 tests for project scope enforcement |
