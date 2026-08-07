# Tier A — Memory observability & hygiene (implementation plan)

## Context

Origin: while removing the redundant IJFW memory layer (2026-08-06→07), the one
capability worth taking from it was **the ability to *see* the corpus.** This plan
implements that, plus two adjacent hygiene wins. It slots **under** the existing
J5 remediation program (w31rd issue #1553 / `2026-07-25-j5-memory-retrieval-audit.md`),
whose finding governs the goal: the corpus is **over-supplied and hard to inspect**
(top-15 memories = 43.4% of retrievals; **29% never retrieved once**). Tier A gives
J5 the standing instrument to find and prune that dead weight — turning the audit
from a one-off study into a live tool.

**Design constraint (anti-lesson from IJFW):** never surface context by mutating
tracked repo files. J5's SessionStart-hook injection stays the mechanism. This work
is inspection/hygiene only — it does not change what gets injected.

**Scope:** A1 + A2 + A3 only. Auto-extraction, profile-derivation, and unscoped
cross-project search were considered and **declined** — they add supply, against #1553.

## Current-state grounding (read from `src/claude_memory`, 2026-08-07)

- **Schema already has the fields we need** (`db/schema.py`): `access_count`,
  `last_accessed`, `source_session`, `supersedes`, `consolidated_from`. → A2 is
  mostly *surfacing existing columns*, not a migration; A3's supersede-graph exists.
- **`update_access` bumps `access_count` + `last_accessed`** (`db/queries.py:296`) —
  so `access_count` IS the retrieval counter; "never retrieved" = `access_count = 0`.
- **`get_stats`** (`db/queries.py:334`) returns only by-type/by-tier/total aggregates.
- **No list/browse query exists** — closest are `get_memories_by_tier`,
  `get_always_load`, `get_stats`. A1 needs a new paginated list query.
- **⚠ The REST API in `api/routes.py` is defined but never served.** No `FastAPI()`
  app assembles it, `server.py` mounts only the MCP `/sse` + `/messages/` Starlette
  routes, and docker-compose runs `--transport sse --port 8787` (MCP only). So A1 is
  **not** "a frontend over an existing API" — it must first stand up an HTTP app that
  hosts the router. This is the largest single piece of A1.
- **`memory_search(summary_only=True)`** already returns `access_count` + timestamps +
  `project_dir` (per its tool schema in `server.py`) — the pattern A2 generalizes.
- **Consolidation infra exists** (`lifecycle/consolidation.py`): `find_clusters`
  (greedy cosine clustering, threshold 0.75) + `run_consolidation` (cold-tier →
  cluster → summary → `consolidated_from` → archive). A3 extends this, reusing `find_clusters`.

---

## A1 — Read-only memory dashboard  ·  top pick

**Goal:** a local web page to browse the whole corpus and prune it, with the audit's
problem-sets as one-click views (never-retrieved, top-N by retrieval, unscoped).

1. **Stand up the REST app** (the missing piece). Add `api/app.py` with
   `create_app() -> FastAPI` that does `app.include_router(routes.router)` and mounts
   static assets. Decide hosting: either (a) run it as a second uvicorn service in
   docker-compose on its own port (simplest, keeps MCP process clean), or (b) mount it
   into `server.py`'s Starlette app alongside `/sse`. **Recommend (a)** — a separate
   `--transport api` / dashboard service, so the MCP stdio/sse path is untouched.
2. **Add a list endpoint** — `GET /api/v1/memories` with `sort` (`access_count` |
   `importance` | `created_at`), `order`, `filter` (`never_retrieved` |
   `unscoped` | `tier=` | `type=`), `limit`, `offset`. Back it with a new
   `list_memories(...)` in `db/queries.py` (plain SELECT + ORDER BY + LIMIT/OFFSET;
   never-retrieved = `WHERE access_count = 0`; unscoped = `WHERE project_dir IS NULL`).
3. **Static dashboard page** — one self-contained `dashboard/index.html` (no build
   step) served by the app. Table sortable by the columns above; preset filter chips
   for never-retrieved / top-15 / unscoped; per-row `forget` (archive) and `importance`
   edit wired to the existing `DELETE`/`PATCH /memories/{id}` routes; a small header
   reading the extended `/stats` (A2). Read-mostly; confirm dialog on any mutation.

**Effort:** MED (mostly the app-hosting + list endpoint; the page is small).
**Risk:** low — additive; no change to MCP path or injection.

## A2 — Provenance + retrieval-stats surfacing  ·  enables A1's pruning

**Goal:** expose the signals the audit prunes on; make "why do I know this?" answerable.

1. **Widen `SearchResultItem`** (`api/routes.py:59`) and `_search_result_to_dict`
   (`mcp/tools.py:71`) to include `access_count`, `last_accessed`, `source_session`,
   `project_dir`. (Full-result parity with the existing `summary_only` path.)
2. **Extend `get_stats`** (`db/queries.py:334`) + `StatsResponse` with the audit's
   headline aggregates: `never_retrieved` count, `top_n_share` (retrieval concentration),
   `unscoped` count. These become the dashboard header and make regressions visible.
3. **`memory_why(id)`** — thin read: return a memory's `source_session`, `created_at`,
   `access_count`, `last_accessed`, `supersedes`/`consolidated_from` lineage. Expose as
   both an MCP tool and `GET /api/v1/memories/{id}/why`. No new storage — all fields exist.

**Effort:** LOW (surfacing existing columns). **Risk:** low.

## A3 — Contradiction-reconciliation in consolidate  ·  corpus hygiene

**Goal:** stop near-duplicate/contradictory memories accumulating — supersede stale
with current instead of leaving both retrievable.

- Extend `lifecycle/consolidation.py`: reuse `find_clusters` to detect high-similarity
  pairs, then split behavior — near-identical → existing summary/merge path;
  **conflicting** (high similarity, diverging `updated_at`/importance) → set the newer
  as `supersedes` the older and archive the older (never hard-delete; the pointer graph
  already supports this via `supersedes` + `update_tiers`).
- Surface reconciliation candidates in the A1 dashboard for human confirm before any
  supersede — do **not** auto-supersede silently.

**Scope discipline (per #1553):** this is corpus hygiene, **not** an enforcement fix.
The audit's evidence is explicit that reconciling *prose* did not stop rule recurrence;
recurring-rule enforcement is #1553's deterministic gates (M1), out of scope here.

**Effort:** MED. **Risk:** MED — merging is destructive-adjacent; supersede-links only,
human-confirm on conflicts.

---

## Build sequence

1. **A2 first** (low effort, unblocks everything): widen result/stats surfaces so the
   data the dashboard needs is reachable over HTTP.
2. **A1** (the instrument): stand up the REST app + list endpoint + static page.
3. **A3 last** (highest risk): fold reconciliation into consolidate, with the A1
   confirm-surface already in place.

## Verification

- **A2:** `POST /api/v1/memories/search` returns `access_count`/`source_session`;
  `GET /api/v1/stats` includes `never_retrieved`; `memory_why` returns lineage for a
  known id. Unit tests in `tests/` alongside existing query tests.
- **A1:** app serves the dashboard; never-retrieved filter count matches
  `SELECT count(*) FROM memories WHERE access_count = 0`; a `forget` from the UI
  archives the row (tier → archived) and it leaves the default view.
- **A3:** a seeded contradictory pair (same topic, diverging `updated_at`) is flagged;
  confirming it sets `supersedes` on the newer + archives the older; the older stops
  appearing in default search, lineage resolves via `memory_why`.
- Full `pytest` green; no change to MCP stdio/sse behavior (A1 hosts a separate app).

---
*Basis: J5 code read 2026-08-07 (`db/{schema,queries}.py`, `api/routes.py`,
`server.py`, `lifecycle/consolidation.py`). Aligns under w31rd #1553. Tier B
(auto-extract / profile-derivation / unscoped cross-project search) declined as
supply-increasing, against the #1553 audit.*
