"""FastAPI REST API layer for claude-memory.

Mirrors the MCP tools as REST endpoints.  The API shares the same
underlying functions so behaviour is identical regardless of transport.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from claude_memory.mcp.tools import (
    tool_memory_aging,
    tool_memory_consolidate,
    tool_memory_forget,
    tool_memory_recall,
    tool_memory_search,
    tool_memory_stats,
    tool_memory_store,
    tool_memory_update,
)

router = APIRouter(prefix="/api/v1", tags=["memory"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class StoreRequest(BaseModel):
    """Payload for storing a new memory."""

    content: str
    type: str = Field(pattern="^(user|feedback|project|reference|lesson)$")
    tags: list[str] | None = None
    importance: float = Field(default=5.0, ge=0.0, le=10.0)
    project_dir: str | None = None
    source_session: str | None = None
    metadata: dict | None = None


class StoreResponse(BaseModel):
    """Result of a store operation — indicates insert or merge."""

    action: str
    memory_id: str
    merged_with: str | None = None


class SearchRequest(BaseModel):
    """Payload for an ad-hoc semantic search."""

    query: str
    project_dir: str | None = None
    top_k: int | None = None


class SearchResultItem(BaseModel):
    """A single search hit with scores."""

    id: str
    content: str
    type: str
    score: float
    tier: str
    importance: float
    tags: list[str]


class SearchResponse(BaseModel):
    """Wrapper for search/recall results."""

    results: list[SearchResultItem]
    count: int


class RecallRequest(BaseModel):
    """Payload for session-start recall."""

    project_dir: str | None = None
    initial_context: str = ""
    top_k: int | None = None


class UpdateRequest(BaseModel):
    """Partial-update payload for an existing memory."""

    content: str | None = None
    importance: float | None = Field(default=None, ge=0.0, le=10.0)
    tags: list[str] | None = None
    type: str | None = Field(
        default=None, pattern="^(user|feedback|project|reference|lesson)$"
    )


class ForgetRequest(BaseModel):
    """Options for the forget (delete) endpoint."""

    archive: bool = True


class StatsResponse(BaseModel):
    """Aggregate memory statistics."""

    by_type: dict[str, int]
    by_tier: dict[str, int]
    total: int


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/memories", response_model=StoreResponse)
async def store_memory(request: StoreRequest) -> StoreResponse:
    """Store a new memory with automatic near-duplicate detection."""
    result: dict = await tool_memory_store(**request.model_dump(exclude_none=True))
    return StoreResponse(**result)


@router.post("/memories/search", response_model=SearchResponse)
async def search(request: SearchRequest) -> SearchResponse:
    """Run an ad-hoc multi-signal search across all memories."""
    result: dict = await tool_memory_search(**request.model_dump(exclude_none=True))
    return SearchResponse(results=result["results"], count=len(result["results"]))


@router.post("/memories/recall", response_model=SearchResponse)
async def recall(request: RecallRequest) -> SearchResponse:
    """Session-start recall — load baseline context and relevant memories."""
    result: dict = await tool_memory_recall(**request.model_dump(exclude_none=True))
    return SearchResponse(results=result["results"], count=len(result["results"]))


@router.patch("/memories/{memory_id}")
async def update(memory_id: str, request: UpdateRequest) -> dict:
    """Partially update an existing memory's fields."""
    fields: dict = request.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    result: dict = await tool_memory_update(memory_id=memory_id, **fields)
    return result


@router.delete("/memories/{memory_id}")
async def forget(memory_id: str, request: ForgetRequest | None = None) -> dict:
    """Delete (or archive) a memory by ID."""
    archive: bool = request.archive if request else True
    result: dict = await tool_memory_forget(memory_id=memory_id, archive=archive)
    return result


@router.post("/maintenance/consolidate")
async def consolidate() -> dict:
    """Trigger a consolidation pass on cold-tier memories."""
    return await tool_memory_consolidate()


@router.post("/maintenance/aging")
async def aging() -> dict:
    """Run an aging cycle: importance decay followed by tier updates."""
    return await tool_memory_aging()


@router.get("/stats", response_model=StatsResponse)
async def stats() -> StatsResponse:
    """Return aggregate counts grouped by type, tier, and total."""
    result: dict = await tool_memory_stats()
    return StatsResponse(**result)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@router.get("/health")
async def health() -> dict:
    """Simple liveness probe."""
    return {"status": "ok", "service": "claude-memory"}
