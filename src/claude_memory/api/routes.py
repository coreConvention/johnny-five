"""FastAPI REST API layer for claude-memory.

Mirrors the MCP tools as REST endpoints.  The API shares the same
underlying functions so behaviour is identical regardless of transport.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from claude_memory.api.graph import EDGE_MODES, PROJECTIONS, GraphError, build_graph
from claude_memory.config import MemorySettings, get_settings
from claude_memory.db.connection import get_connection
from claude_memory.db.queries import ListQueryError
from claude_memory.lifecycle.consolidation import ReconciliationError
from claude_memory.mcp.tools import (
    tool_memory_aging,
    tool_memory_consolidate,
    tool_memory_forget,
    tool_memory_get,
    tool_memory_list,
    tool_memory_recall,
    tool_memory_search,
    tool_memory_stats,
    tool_memory_store,
    tool_memory_update,
    tool_memory_why,
    tool_reconciliation_apply,
    tool_reconciliation_candidates,
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
    """A single search hit with scores and provenance signals.

    The provenance fields (``access_count``, ``last_accessed``,
    ``source_session``, ``project_dir``) bring the full REST result to parity
    with the MCP serializer so the dashboard (A1) can prune on them. Without
    them declared here, FastAPI silently drops the serializer's provenance keys
    and the surfacing is a no-op.
    """

    id: str
    content: str
    type: str
    score: float
    tier: str
    importance: float
    tags: list[str]
    access_count: int
    last_accessed: str
    source_session: str | None = None
    project_dir: str | None = None


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
    """Aggregate memory statistics, including the audit's headline signals."""

    by_type: dict[str, int]
    by_tier: dict[str, int]
    total: int
    # Tier A / A2 headline aggregates — the dashboard header and regression
    # tripwires: never_retrieved (access_count == 0), unscoped (project_dir IS
    # NULL), top_n_share (fraction of all retrievals in the top-N memories).
    never_retrieved: int
    unscoped: int
    top_n_share: float


class MemoryListItem(BaseModel):
    """A single row in the dashboard browse view (A1)."""

    id: str
    content: str
    type: str
    tags: list[str]
    importance: float
    tier: str
    access_count: int
    last_accessed: str
    created_at: str
    source_session: str | None = None
    project_dir: str | None = None


class MemoryListResponse(BaseModel):
    """Wrapper for the paginated browse list."""

    results: list[MemoryListItem]
    count: int


class ReconcileApplyRequest(BaseModel):
    """Payload confirming one reconciliation: keep ``newer``, supersede+archive ``older``."""

    newer_id: str
    older_id: str


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


@router.get("/memories", response_model=MemoryListResponse)
async def list_memories_endpoint(
    sort: str = Query("created_at", description="access_count | importance | created_at | last_accessed"),
    order: str = Query("desc", description="asc | desc"),
    filter: str | None = Query(None, description="never_retrieved | unscoped"),
    tier: str | None = Query(None, description="hot | warm | cold | archived"),
    type: str | None = Query(None, description="user | feedback | project | reference | lesson"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    include_archived: bool = Query(False),
) -> MemoryListResponse:
    """Browse the corpus (dashboard, A1).

    ``sort`` / ``order`` / ``filter`` are whitelist-validated — an unknown value
    is a 400, never interpolated into SQL.
    """
    try:
        result: dict = await tool_memory_list(
            sort=sort,
            order=order,
            filter=filter,
            tier=tier,
            type=type,
            limit=limit,
            offset=offset,
            include_archived=include_archived,
        )
    except ListQueryError as exc:
        # Only whitelist-validation failures are 400s. A json decode error from a
        # corrupt row is also a ValueError but must surface as a 500, not be
        # mislabelled a bad request — so we catch ListQueryError specifically.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MemoryListResponse(**result)


@router.get("/memories/{memory_id}")
async def get_one(memory_id: str) -> dict:
    """Fetch a single memory in full by ID — read-only.

    Does not count as a retrieval: ``access_count`` is left untouched so that
    inspecting the corpus cannot skew the frequency signal that ranks it.
    """
    result: dict = await tool_memory_get(memory_id=memory_id)
    if not result.get("found", False):
        raise HTTPException(status_code=404, detail="Memory not found")
    return result


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
    """Archive a memory by ID (soft delete → ``archived`` tier).

    Hard delete is **not permitted** over this HTTP surface: the dashboard's
    "forget" only ever archives, and Tier A design §2 forbids hard-deleting a
    memory. An explicit ``archive=false`` is rejected rather than silently
    honoured, so a served, unauthenticated port can never destroy a memory.
    (The archival/``supersedes`` graph remains the only removal mechanism.)
    """
    if request is not None and request.archive is False:
        raise HTTPException(
            status_code=400,
            detail="Hard delete is not permitted; memories can only be archived.",
        )
    result: dict = await tool_memory_forget(memory_id=memory_id, archive=True)
    return result


@router.get("/memories/{memory_id}/why")
async def why(memory_id: str) -> dict:
    """Explain why a memory is known — its provenance and lineage (no content).

    Read-only; does not count as a retrieval. Returns 404 if the id is unknown.
    """
    result: dict = await tool_memory_why(memory_id=memory_id)
    if not result.get("found", False):
        raise HTTPException(status_code=404, detail="Memory not found")
    return result


@router.get("/reconciliation/candidates")
async def reconciliation_candidates(
    limit: int = Query(100, ge=1, le=500),
    similarity_threshold: float = Query(0.85, ge=0.0, le=1.0),
) -> dict:
    """List contradiction-reconciliation candidates (A3) — read-only.

    High-similarity, diverging pairs surfaced for human review; nothing is
    superseded until an explicit confirm hits ``POST /reconciliation/apply``.
    """
    return await tool_reconciliation_candidates(
        similarity_threshold=similarity_threshold, limit=limit
    )


@router.post("/reconciliation/apply")
async def reconciliation_apply(request: ReconcileApplyRequest) -> dict:
    """Apply a human-confirmed reconciliation: newer supersedes older, older archived.

    Never hard-deletes. Returns 400 on an invalid request (unknown id, pinned,
    already-archived older, or inverted direction).
    """
    try:
        return await tool_reconciliation_apply(
            newer_id=request.newer_id, older_id=request.older_id
        )
    except ReconciliationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
# 3D graph
# ---------------------------------------------------------------------------


@router.get("/graph")
def graph(
    # Whitelist-validated at the boundary as well as in build_graph: the
    # projection name becomes a path segment in the on-disk layout cache key,
    # so it must never be free-form user input.
    projection: str = Query(
        "tsne",
        pattern=f"^({'|'.join(PROJECTIONS)})$",
        description=" | ".join(PROJECTIONS),
    ),
    edges: str = Query(
        "semantic",
        pattern=f"^({'|'.join(EDGE_MODES)})$",
        description=" | ".join(EDGE_MODES),
    ),
    threshold: float = Query(0.6, ge=-1.0, le=1.0),
    k: int = Query(8, ge=1, le=32),
) -> dict:
    """Return the 3D layout of the whole corpus plus one mode of derived edges.

    Deliberately a **sync** ``def``. FastAPI runs sync handlers in a threadpool,
    so a cold projection (~5 s for t-SNE, longer for UMAP's first JIT) cannot
    block the event loop and freeze the rest of the dashboard while it runs.

    Always returns every memory, archived included: positions are computed once
    over the whole corpus and filtering happens client-side, so that narrowing
    the view never moves a point. An unknown projection or edge mode is a 400.
    """
    settings: MemorySettings = get_settings()
    db_path = settings.resolve_db_path()
    conn = get_connection(db_path, settings.embedding_dim)
    try:
        return build_graph(
            conn,
            db_path,
            projection=projection,
            edges=edges,
            threshold=threshold,
            k=k,
        )
    except GraphError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@router.get("/health")
async def health() -> dict:
    """Simple liveness probe."""
    return {"status": "ok", "service": "claude-memory"}
