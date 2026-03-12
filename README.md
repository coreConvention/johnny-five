# johnny-five

> "Need more input!" — *Short Circuit* (1986)

Persistent memory system for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Gives Claude unlimited long-term memory that persists across conversations with semantic search, automatic deduplication, and intelligent aging.

Built on SQLite + FTS5 + [sqlite-vec](https://github.com/asg017/sqlite-vec) with local embeddings via [sentence-transformers](https://www.sbert.net/).

## Quick Start

### Docker (recommended)

```bash
# Build
docker build -t johnny-five:latest .

# Run (stdio transport for Claude Code MCP)
docker run -d --name johnny-five -i \
  -v johnny-five-data:/data \
  johnny-five:latest --transport stdio
```

### Add to your project

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "johnny-five": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-v", "johnny-five-data:/data",
        "johnny-five:latest"
      ],
      "env": {
        "MEMORY_DB_PATH": "/data/memory.db"
      }
    }
  }
}
```

Then restart Claude Code. The memory tools will be available automatically.

## MCP Tools

| Tool | Description |
|------|-------------|
| `memory_store` | Store a memory with automatic near-duplicate detection and merge |
| `memory_search` | Multi-signal retrieval: semantic similarity + recency + frequency + importance |
| `memory_recall` | Session-start recall — loads high-importance memories + semantically relevant context |
| `memory_update` | Update content, importance, tags, or type (content changes re-embed automatically) |
| `memory_forget` | Archive (soft delete) or permanently delete a memory |
| `memory_consolidate` | Cluster and summarize cold-tier memories |
| `memory_stats` | Database statistics by type and tier |
| `memory_aging` | Run importance decay and tier re-evaluation cycle |

### Memory Types

| Type | Purpose |
|------|---------|
| `user` | User profile, preferences, expertise level |
| `feedback` | Corrections and behavioral guidance |
| `project` | Project context, goals, decisions |
| `reference` | Pointers to external resources |
| `lesson` | Mistakes made and rules to prevent them |

## How It Works

### Multi-Signal Retrieval

Every search combines four signals into a composite score:

| Signal | Weight | Description |
|--------|--------|-------------|
| Semantic similarity | 0.45 | Cosine similarity between query and memory embeddings |
| Recency | 0.20 | Exponential decay based on days since last access |
| Frequency | 0.10 | Log-scaled access count |
| Importance | 0.25 | User-assigned score (0-10), decays daily at 0.995x |

### Three-Tier Aging

Memories move through tiers based on access patterns and importance:

```
hot  →  warm  →  cold  →  archived
 ↑       ↑       ↑
 └───────┴───────┘  (re-promoted on access or importance increase)
```

- **Hot**: Frequently accessed or recently created. No similarity threshold.
- **Warm**: Not accessed in 30+ days. Must exceed 0.75 similarity to surface.
- **Cold**: Low importance, not accessed in 180+ days. Must exceed 0.90 similarity.
- **Archived**: Effectively deleted but recoverable. Not returned in searches.

### Dedup on Write

When storing a memory, the system checks for near-duplicates (cosine distance < 0.15). If found, it merges the new content into the existing memory, bumps importance, re-embeds, and returns `"action": "merged"` instead of `"inserted"`.

## Configuration

All settings are configurable via `MEMORY_*` environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_DB_PATH` | `~/.claude/memory.db` | SQLite database path |
| `MEMORY_MODEL_NAME` | `all-MiniLM-L6-v2` | Embedding model (384-dim) |
| `MEMORY_TOP_K` | `15` | Default search result count |
| `MEMORY_DEDUP_THRESHOLD` | `0.15` | Cosine distance for near-duplicate detection |
| `MEMORY_ALPHA` | `0.45` | Semantic similarity weight |
| `MEMORY_BETA` | `0.20` | Recency weight |
| `MEMORY_GAMMA` | `0.10` | Frequency weight |
| `MEMORY_DELTA` | `0.25` | Importance weight |
| `MEMORY_DECAY_RATE` | `0.995` | Daily importance decay multiplier |
| `MEMORY_SERVER_PORT` | `8787` | SSE transport port |

## Alternative Transports

### SSE (HTTP)

```bash
# Docker
docker run -d --name johnny-five \
  -p 8787:8787 \
  -v johnny-five-data:/data \
  johnny-five:latest --transport sse --port 8787

# Docker Compose
docker compose up -d
```

### Native Python

```bash
pip install -e .
johnny-five                          # stdio (default)
johnny-five --transport sse --port 8787  # SSE/HTTP
```

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests (103 tests)
pytest

# Run tests with coverage
pytest --cov=claude_memory
```

## Making It Reliable

Johnny-five works best when Claude knows to use it automatically. The `setup/` directory contains ready-to-use snippets.

### 1. Add to your CLAUDE.md

Copy `setup/CLAUDE.md.snippet` into your global `~/.claude/CLAUDE.md` (applies to all projects) or a project-specific `CLAUDE.md`. This tells Claude when to store, search, and recall memories.

**Linux/macOS:**
```bash
cat setup/CLAUDE.md.snippet >> ~/.claude/CLAUDE.md
```

**Windows (PowerShell):**
```powershell
Get-Content setup\CLAUDE.md.snippet | Add-Content $env:USERPROFILE\.claude\CLAUDE.md
```

### 2. Add hooks for auto-recall and auto-save

Hooks make memory fully automatic — recall at session start, save learnings at session end. Merge `setup/hooks.json.snippet` into your Claude Code settings.

**Where to put hooks:**

- **Global** (all projects): `~/.claude/settings.json`
- **Project-specific**: `.claude/settings.json` in your repo

If you already have a `settings.json`, merge the `hooks` key. If not, copy the snippet as-is:

```bash
# Global (creates file if it doesn't exist)
cp setup/hooks.json.snippet ~/.claude/settings.json

# Or for a specific project
cp setup/hooks.json.snippet /path/to/project/.claude/settings.json
```

> **How it works:** The `SessionStart` hook prompts Claude to call `memory_recall` at the beginning of every conversation. The `Stop` hook prompts Claude to review the conversation for anything worth storing before finishing. Both are silent no-ops if the johnny-five MCP server isn't connected.

### 3. Add MCP config to your project

Add to `.mcp.json` in your project root (see [Quick Start](#quick-start) above). This is per-project — each project that should use memory needs this entry.

### 4. Verify it works

Start a Claude Code session in your project and ask:

```
Use memory_stats to check if johnny-five is connected
```

You should see `{"by_type": {}, "by_tier": {}, "total": 0}` for a fresh database.

## Architecture

```
src/claude_memory/
├── server.py              # MCP server entrypoint (stdio + SSE)
├── config.py              # Pydantic settings (MEMORY_* env vars)
├── db/
│   ├── connection.py      # SQLite connection (WAL, sqlite-vec, pragmas)
│   ├── schema.py          # DDL: memories table, FTS5, vec0, indexes
│   └── queries.py         # Typed CRUD, FTS search, vector search
├── embeddings/
│   └── encoder.py         # Thread-safe sentence-transformers wrapper
├── retrieval/
│   ├── scorer.py          # 4-signal weighted scoring
│   ├── reranker.py        # Candidate merging + tier-aware filtering
│   └── search.py          # Full retrieval pipeline orchestrator
├── lifecycle/
│   ├── dedup.py           # Near-duplicate detection + merge-on-store
│   ├── aging.py           # Importance decay + tier transitions
│   └── consolidation.py   # Cold memory clustering + summarization
├── mcp/
│   ├── tools.py           # MCP tool implementations
│   └── resources.py       # MCP resource endpoints
└── api/
    └── routes.py          # FastAPI REST mirror of MCP tools
```

## License

MIT
