# Claude Memory System — Architecture Design

> **Status**: Draft
> **Created**: 2026-03-11
> **Author**: Brandon + Claude

## Problem Statement

Claude Code's context window is finite. Memories, lessons, preferences, and project
knowledge accumulated across conversations are either lost or managed via flat markdown
files that must be fully loaded every time. We need a system that stores unbounded
knowledge and retrieves only what's relevant — fast enough to feel invisible.

## Design Principles

1. **Retrieval over storage** — storing data is trivial; finding the right 15 memories out of 100k is the hard problem
2. **Consolidate, don't delete** — aging means summarizing, not throwing away
3. **Zero friction** — must integrate natively with Claude Code, no manual lookups
4. **Local-first** — works offline on Windows, Docker-optional, Azure-optional
5. **Single-file database** — backup = copy one file

---

## Technology Stack

### Core

| Component | Choice | Rationale |
|-----------|--------|-----------|
| **Language** | Python 3.12+ | Best embedding ecosystem (sentence-transformers), MCP SDK support, SQLite is native |
| **Database** | SQLite (WAL mode) | Single-file, zero-infra, hybrid search, battle-tested |
| **Full-text search** | FTS5 (built into SQLite) | Keyword matching for multi-signal retrieval |
| **Vector search** | sqlite-vec | Embedding similarity search within SQLite |
| **Embeddings** | all-MiniLM-L6-v2 (sentence-transformers) | 384-dim, ~2ms/embed on CPU, free, private, local |
| **Integration** | MCP Server (stdio + SSE) | Native Claude Code integration via `.mcp.json` |
| **API** (optional) | FastAPI + Uvicorn | REST interface for non-MCP consumers |
| **Containerization** | Docker | Portable, reproducible, Azure-deployable |

### Why Python Over C# or TypeScript

- **Embedding ecosystem**: `sentence-transformers` is the gold standard. C# requires ONNX Runtime + manual model export. TypeScript has no good local embedding story.
- **sqlite-vec**: First-class Python bindings. C# and TS would need FFI/native interop.
- **MCP SDK**: Python `mcp` package is mature and well-documented.
- **Trade-off accepted**: Python is slower than C#/.NET for raw throughput, but the bottleneck is LLM inference (seconds), not memory retrieval (~25ms). The ecosystem advantage outweighs the performance gap.

### Upgrade Paths

| When | Swap | Why |
|------|------|-----|
| Retrieval quality insufficient | all-MiniLM-L6-v2 → nomic-embed-text (768-dim via Ollama) | Better semantic discrimination |
| Scale > 1M memories | sqlite-vec → LanceDB | Columnar, zero-copy, built for large-scale vector search |
| Azure production deploy | SQLite → Azure Cosmos DB (MongoDB vCore) with vector search | Managed, multi-region, no volume mounts |
| Need GPU embeddings | sentence-transformers CPU → ONNX Runtime with CUDA | 10x faster embedding generation |

---

## Schema

```sql
-- Core memory table
CREATE TABLE memories (
    id              TEXT PRIMARY KEY,       -- ULID (time-sortable, globally unique)
    content         TEXT NOT NULL,          -- The actual memory text
    summary         TEXT,                   -- Compressed version for cold-tier display
    type            TEXT NOT NULL,          -- user | feedback | project | reference | lesson
    tags            TEXT DEFAULT '[]',      -- JSON array of tags
    created_at      TEXT NOT NULL,          -- ISO 8601
    updated_at      TEXT NOT NULL,          -- ISO 8601
    last_accessed   TEXT NOT NULL,          -- ISO 8601, updated on retrieval
    access_count    INTEGER DEFAULT 0,      -- Bumped each retrieval
    importance      REAL DEFAULT 5.0,       -- 0.0–10.0, decays over time
    tier            TEXT DEFAULT 'hot',     -- hot | warm | cold | archived
    project_dir     TEXT,                   -- Scoped to a working directory (nullable = global)
    source_session  TEXT,                   -- Conversation that created this memory
    supersedes      TEXT,                   -- ID of memory this one replaced
    consolidated_from TEXT DEFAULT '[]',    -- JSON array of IDs that were merged into this
    metadata        TEXT DEFAULT '{}',      -- Extensible JSON blob
    CHECK (type IN ('user', 'feedback', 'project', 'reference', 'lesson')),
    CHECK (tier IN ('hot', 'warm', 'cold', 'archived')),
    CHECK (importance >= 0.0 AND importance <= 10.0)
);

-- Full-text search (keyword matching)
CREATE VIRTUAL TABLE memories_fts USING fts5(
    content, summary, tags,
    content='memories',
    content_rowid='rowid'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, summary, tags)
    VALUES (new.rowid, new.content, new.summary, new.tags);
END;
CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, summary, tags)
    VALUES ('delete', old.rowid, old.content, old.summary, old.tags);
END;
CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, summary, tags)
    VALUES ('delete', old.rowid, old.content, old.summary, old.tags);
    INSERT INTO memories_fts(rowid, content, summary, tags)
    VALUES (new.rowid, new.content, new.summary, new.tags);
END;

-- Vector similarity index (sqlite-vec, 384 dimensions for MiniLM)
CREATE VIRTUAL TABLE memories_vec USING vec0(
    id TEXT PRIMARY KEY,
    embedding float[384]
);

-- Performance indexes
CREATE INDEX idx_memories_type_importance ON memories(type, importance DESC);
CREATE INDEX idx_memories_tier ON memories(tier);
CREATE INDEX idx_memories_project ON memories(project_dir);
CREATE INDEX idx_memories_last_accessed ON memories(last_accessed);
CREATE INDEX idx_memories_created ON memories(created_at);
CREATE INDEX idx_memories_supersedes ON memories(supersedes);
```

---

## Retrieval Algorithm

### Multi-Signal Fusion

Single-signal search gives mediocre results. We combine four signals:

```
score = α * semantic_similarity
      + β * recency_score
      + γ * frequency_score
      + δ * importance

Where:
  α = 0.45  (semantic relevance dominates)
  β = 0.20  (recent = more likely relevant)
  γ = 0.10  (frequently accessed = proven value)
  δ = 0.25  (explicit importance rating)

  recency_score  = e^(-0.01 * days_since_last_access)   # half-life ~70 days
  frequency_score = min(log2(access_count + 1) / 10, 1)  # capped at 1.0
  semantic_similarity = 1 - cosine_distance               # 0.0–1.0
```

### Query Flow

```
┌─ User message + working directory ─┐
│                                     │
▼                                     │
Generate embedding of context ────────┘
│
├──► Vector search (sqlite-vec)
│    SELECT id, vec_distance_cosine(embedding, ?) as dist
│    FROM memories_vec
│    ORDER BY dist LIMIT 50
│
├──► FTS5 keyword search
│    SELECT rowid, rank FROM memories_fts
│    WHERE memories_fts MATCH ?
│    ORDER BY rank LIMIT 50
│
├──► Always-load query
│    SELECT * FROM memories
│    WHERE type = 'user' AND importance > 7.0
│    OR (project_dir = ? AND tier = 'hot')
│
▼
Merge candidate sets → deduplicate by id
│
▼
Score each candidate with multi-signal formula
│
▼
Return top-k (configurable, default 15)
│
▼
UPDATE memories SET
  last_accessed = datetime('now'),
  access_count = access_count + 1
WHERE id IN (returned_ids)
```

### Tier-Aware Filtering

| Tier | Retrieval behavior |
|------|--------------------|
| **hot** | Always in candidate pool |
| **warm** | Only if semantic_similarity > 0.75 |
| **cold** | Only if semantic_similarity > 0.90 |
| **archived** | Never auto-retrieved; manual lookup only |

---

## Aging & Consolidation

### Importance Decay

Run nightly (or on session start if >24h since last run):

```sql
-- Decay all memories not accessed in the current session
-- Decay rate: 0.5% per day → half-life ~138 days
UPDATE memories
SET importance = MAX(importance * 0.995, 0.1),  -- floor at 0.1, never fully zero
    updated_at = datetime('now')
WHERE last_accessed < datetime('now', '-1 day')
  AND tier != 'archived';
```

### Tier Promotion/Demotion

Run weekly:

```python
# Promote to hot: accessed 3+ times in last 30 days
UPDATE memories SET tier = 'hot'
WHERE access_count >= 3
  AND last_accessed > datetime('now', '-30 days')
  AND tier != 'hot';

# Demote to warm: not accessed in 30 days
UPDATE memories SET tier = 'warm'
WHERE last_accessed < datetime('now', '-30 days')
  AND tier = 'hot';

# Demote to cold: not accessed in 180 days AND low importance
UPDATE memories SET tier = 'cold'
WHERE last_accessed < datetime('now', '-180 days')
  AND importance < 3.0
  AND tier = 'warm';
```

### Consolidation (Monthly)

The key insight: cold memories aren't deleted — they're **summarized and merged**.

```python
async def consolidate_cold_memories():
    """
    1. Cluster cold memories by semantic similarity
    2. For each cluster of 5+ memories:
       a. Generate a summary using the LLM
       b. Create one new 'consolidated' memory with:
          - importance = max(cluster importances)
          - consolidated_from = [list of original IDs]
       c. Move originals to 'archived' tier
    3. For isolated cold memories (no cluster):
       a. If importance < 1.0 and access_count < 2: archive directly
       b. Otherwise: leave as cold
    """
```

### Deduplication (On Every Write)

```python
async def store_memory(content: str, type: str, **kwargs) -> str:
    embedding = embed(content)

    # Check for near-duplicates (cosine distance < 0.15)
    similar = query_vec(embedding, threshold=0.15, limit=5)

    if similar:
        # Merge into the most similar existing memory
        target = similar[0]
        target.content = merge_content(target.content, content)
        target.importance = min(target.importance + 1.0, 10.0)
        target.updated_at = now()
        target.embedding = embed(target.content)  # re-embed merged content
        return target.id

    # No duplicate — insert new
    return insert_memory(content, embedding, type, **kwargs)
```

---

## MCP Server Interface

Claude Code connects to this as an MCP server. Configured in `.mcp.json`:

```json
{
  "mcpServers": {
    "memory": {
      "command": "python",
      "args": ["-m", "claude_memory.server"],
      "env": {
        "MEMORY_DB_PATH": "C:/Users/Brandon/.claude/memory.db",
        "MEMORY_MODEL": "all-MiniLM-L6-v2"
      }
    }
  }
}
```

Or via Docker:

```json
{
  "mcpServers": {
    "memory": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-v", "C:/Users/Brandon/.claude:/data",
        "claude-memory:latest"
      ]
    }
  }
}
```

### MCP Tools Exposed

| Tool | Description |
|------|-------------|
| `memory_store` | Store a new memory (dedup-aware) |
| `memory_search` | Multi-signal retrieval given a query string |
| `memory_recall` | Load all memories for session start (user prefs + project context) |
| `memory_update` | Update an existing memory's content or importance |
| `memory_forget` | Archive or delete a specific memory |
| `memory_consolidate` | Trigger manual consolidation run |
| `memory_stats` | Return counts by type, tier, and age distribution |

### MCP Resources Exposed

| Resource | Description |
|----------|-------------|
| `memory://stats` | Current database statistics |
| `memory://recent` | Last 20 memories created |
| `memory://types/{type}` | All memories of a given type |

---

## Docker Setup

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# System deps for sqlite-vec
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the embedding model at build time (not at runtime)
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

COPY src/ ./src/

ENV MEMORY_DB_PATH=/data/memory.db
ENV MEMORY_MODEL=all-MiniLM-L6-v2

EXPOSE 8787

# Stdio mode for MCP, HTTP mode for REST API
ENTRYPOINT ["python", "-m", "claude_memory.server"]
CMD ["--transport", "stdio"]
```

### requirements.txt

```
mcp>=1.0.0
sentence-transformers>=3.0.0
sqlite-vec>=0.1.0
fastapi>=0.115.0
uvicorn>=0.32.0
ulid-py>=1.1.0
pydantic>=2.0.0
```

### docker-compose.yml (local development)

```yaml
services:
  claude-memory:
    build: .
    ports:
      - "8787:8787"
    volumes:
      - memory-data:/data
    environment:
      - MEMORY_DB_PATH=/data/memory.db
      - MEMORY_MODEL=all-MiniLM-L6-v2
    command: ["--transport", "sse", "--port", "8787"]

volumes:
  memory-data:
    driver: local
```

---

## Azure Deployment (Bonus)

### Option A: Azure Container Apps (Recommended)

Cheapest and simplest. Serverless containers with persistent storage.

```bash
# Create resource group
az group create -n rg-claude-memory -l eastus

# Create Container Apps environment
az containerapp env create \
  -n claude-memory-env \
  -g rg-claude-memory \
  -l eastus

# Create Azure Files share for SQLite persistence
az storage account create -n claudememorystorage -g rg-claude-memory -l eastus --sku Standard_LRS
az storage share create -n memory-data --account-name claudememorystorage

# Mount storage and deploy
az containerapp env storage set \
  -n claude-memory-env \
  -g rg-claude-memory \
  --storage-name memoryvol \
  --azure-file-account-name claudememorystorage \
  --azure-file-share-name memory-data \
  --azure-file-account-key <key> \
  --access-mode ReadWrite

az containerapp create \
  -n claude-memory \
  -g rg-claude-memory \
  --environment claude-memory-env \
  --image <your-acr>.azurecr.io/claude-memory:latest \
  --target-port 8787 \
  --ingress external \
  --min-replicas 0 \
  --max-replicas 1 \
  --cpu 1.0 --memory 2.0Gi \
  --env-vars MEMORY_DB_PATH=/mnt/memory/memory.db \
  --volume-name memoryvol \
  --volume-mount-path /mnt/memory
```

**Cost**: ~$0/month when idle (scales to zero), ~$0.05/hour when active.

### Option B: Azure-Native Upgrade Path

When you outgrow SQLite-in-a-container:

| Component | Azure Service | Why |
|-----------|---------------|-----|
| Vector + document store | Azure Cosmos DB (MongoDB vCore) | Native vector search, managed, multi-region |
| Full-text search | Azure AI Search | BM25 + vector hybrid search, semantic ranking |
| Embeddings | Azure OpenAI (text-embedding-3-small) | Higher quality than local, 1536-dim |
| API hosting | Azure Container Apps | Same as Option A |

This is the "throw money at scale" path. Don't go here until SQLite becomes a bottleneck (likely never for single-user).

---

## Windows Local Development (No Docker)

For running natively on Windows without Docker:

```powershell
# Create virtual environment
python -m venv .venv
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run as MCP server (stdio)
python -m claude_memory.server --transport stdio

# Run as HTTP API
python -m claude_memory.server --transport sse --port 8787
```

SQLite works natively on Windows. No special setup needed.

---

## Project Structure

```
claude-memory/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── pyproject.toml
├── README.md
├── src/
│   └── claude_memory/
│       ├── __init__.py
│       ├── server.py          # MCP server + FastAPI entrypoint
│       ├── config.py          # Settings via pydantic-settings
│       ├── db/
│       │   ├── __init__.py
│       │   ├── schema.py      # DDL, migrations
│       │   ├── connection.py   # SQLite connection pool (WAL mode)
│       │   └── queries.py     # Typed query functions
│       ├── embeddings/
│       │   ├── __init__.py
│       │   └── encoder.py     # SentenceTransformer wrapper, lazy-load model
│       ├── retrieval/
│       │   ├── __init__.py
│       │   ├── scorer.py      # Multi-signal scoring function
│       │   ├── search.py      # Orchestrates vector + FTS + always-load
│       │   └── reranker.py    # Candidate merging and final ranking
│       ├── lifecycle/
│       │   ├── __init__.py
│       │   ├── aging.py       # Importance decay, tier promotion/demotion
│       │   ├── consolidation.py # Cluster + summarize cold memories
│       │   └── dedup.py       # Near-duplicate detection on write
│       ├── mcp/
│       │   ├── __init__.py
│       │   ├── tools.py       # MCP tool definitions (store, search, recall, etc.)
│       │   └── resources.py   # MCP resource definitions (stats, recent, etc.)
│       └── api/
│           ├── __init__.py
│           └── routes.py      # FastAPI REST routes (mirrors MCP tools)
└── tests/
    ├── conftest.py            # In-memory SQLite fixture
    ├── test_dedup.py
    ├── test_retrieval.py
    ├── test_aging.py
    ├── test_consolidation.py
    └── test_mcp_tools.py
```

---

## Performance Expectations (NVMe SSD)

| Operation | Expected Latency |
|-----------|-----------------|
| Single embedding generation (CPU) | ~2ms |
| Vector search across 100k memories | ~5–10ms |
| FTS5 keyword search across 100k | ~1–2ms |
| Combined multi-signal retrieval | ~15–25ms |
| Memory write with dedup check | ~5–10ms |
| Session-start recall (load user prefs + project context) | ~30–50ms |
| Full consolidation pass (10k cold memories) | ~30–60s |

All retrieval operations are sub-50ms — invisible relative to LLM inference.

---

## Migration Path from Current Memory System

The existing flat-file memory system (`C:\Users\Brandon\.claude\projects\*\memory\`)
can be imported:

1. Read each `.md` file from the memory directory
2. Parse frontmatter (name, description, type)
3. Generate embedding from content
4. Insert into SQLite with appropriate type and importance
5. Run dedup pass to merge any overlapping memories

This is a one-time migration, not an ongoing sync.

---

## Open Questions

- [ ] Should consolidation summaries be generated by a local LLM (Ollama) or by Claude API?
- [ ] What's the right `top-k` default? 15 memories ≈ 2–4k tokens — is that too much context?
- [ ] Should the MCP server auto-store memories from conversations, or only when explicitly told?
- [ ] Do we need per-project database files, or one global database with project_dir filtering?
- [ ] Should aging parameters (decay rate, tier thresholds) be user-configurable?

---

## Next Steps

1. Scaffold the Python project with `pyproject.toml` and base dependencies
2. Implement schema creation and connection management
3. Build the embedding encoder (lazy-load model on first use)
4. Implement `memory_store` with dedup detection
5. Implement `memory_search` (multi-signal retrieval)
6. Wire up MCP server with stdio transport
7. Test end-to-end with Claude Code via `.mcp.json`
8. Add Docker support
9. Implement aging and consolidation background jobs
10. (Optional) Azure Container Apps deployment
