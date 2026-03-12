# Lessons Learned

## 2026-03-11

### [Process] Always run the self-improvement loop
- **Mistake**: Started writing architecture doc without proactively following the self-improvement loop from global CLAUDE.md
- **Correction**: User reminded me to use the self-improvement loop
- **Rule**: After ANY task completion or correction, append an entry here. Check this file at session start. Don't wait to be reminded.

### [Architecture] Parallel subagents produce contract mismatches at module boundaries
- **Mistake**: Launched 5 agents in parallel to build independent modules. Each agent made slightly different assumptions about shared interfaces — parameter names (`limit` vs `top_k`, `hot_access` vs `hot_access_threshold`, `id` vs `memory_id`), return types (`None` vs `int`, `list[MemoryRecord]` vs `list[str]`), and call signatures (`update_access` single vs list).
- **Correction**: Had to do a full integration pass reading all 10+ files and fixing ~10 contract mismatches across queries.py, search.py, reranker.py, aging.py, consolidation.py, and dedup.py.
- **Rule**: When parallelizing module creation via subagents, define a **shared interface contract** (function signatures with exact parameter names, types, and return types) upfront and include it in every agent's prompt. Alternatively, build the shared interface layer first (e.g., queries.py) and pass its exact code to dependent agents.

### [Communication] Don't present Docker vs native Python as a choice when it's not one
- **Mistake**: Asked Brandon "Would you rather start with native Python or Docker?" when the entire project was already set up for Docker and there was no reason to present alternatives. Created confusion.
- **Correction**: Brandon said "I am confused by your question."
- **Rule**: Don't present unnecessary choices. If there's a clear default path (Docker was already built, Dockerfile exists, user asked about Docker), just do it. Only ask when there's a genuine trade-off the user needs to weigh in on.

### [Integration] Know your library's distance metric before writing comparison logic
- **Mistake**: Assumed sqlite-vec's `vec0` virtual table returns **cosine distance** (0–1). It actually returns **L2 (Euclidean) distance** (0–~2 for normalized vectors). The reranker computed `similarity = 1.0 - L2_dist`, which went negative for all real queries and clamped to 0.0, making semantic scoring completely non-functional. This also broke dedup (threshold calibrated for cosine distance vs L2).
- **Correction**: Added `_l2_to_cosine_distance()` conversion (`cos_dist = L2²/2` for unit vectors) applied in `search_vec()` so all downstream consumers get correct cosine distances.
- **Rule**: When integrating any vector search library, **verify the distance metric** it returns (L2, cosine, inner product) with a simple test before writing scoring/threshold logic. Don't assume — check the docs and validate empirically.

### [Data] Don't JSON-encode fields that will be JSON-encoded again downstream
- **Mistake**: `dedup.py` passed `json.dumps(tags)` into `MemoryRecord.tags`, but `insert_memory()` also calls `json.dumps(record.tags)`. Result: double-encoded JSON strings like `"[\"tag1\"]"` in the database instead of `["tag1"]`.
- **Correction**: Pass raw Python objects (lists, dicts) to `MemoryRecord`; let `insert_memory` handle the single JSON serialization.
- **Rule**: Trace the full data path from construction → serialization → storage. Only serialize at the boundary (the function that writes to the database), never before.

### [Security] Sanitize user input for FTS5 MATCH queries
- **Mistake**: Passed raw user queries to SQLite FTS5 MATCH clause. Characters like `?`, `*`, `"`, `(`, `)` have special meaning in FTS5 syntax and cause `OperationalError: fts5: syntax error`.
- **Correction**: Added `_sanitize_fts_query()` that strips special characters and wraps each token in double quotes for literal matching.
- **Rule**: Any user-facing full-text search must sanitize input for the specific FTS engine's query syntax. This is analogous to SQL injection prevention — never pass raw input to a MATCH clause.
