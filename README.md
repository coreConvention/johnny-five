# johnny-five

> "Need more input!" — *Short Circuit* (1986)

Persistent memory system for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Gives Claude unlimited long-term memory that persists across conversations with semantic search, automatic deduplication, and intelligent aging.

Built on SQLite + FTS5 + [sqlite-vec](https://github.com/asg017/sqlite-vec) with local embeddings via [sentence-transformers](https://www.sbert.net/).

> **Adopting this in your own environment?**
>
> - **The fast path — let Claude do it:** clone this repo, open Claude Code in the clone, and run [`/integrate`](.claude/commands/integrate.md). It detects what's already set up, asks two questions, and wires the rest end to end (Docker container, hooks, global CLAUDE.md discipline rules, optional per-project wiring, verification). Idempotent and re-run-safe.
> - **Don't have it cloned yet?** Use the [Canned Claude prompt](#canned-claude-prompt) below — paste into a fresh Claude Code session and it'll handle the clone + setup.
> - **Want to read first or set up by hand?** [`docs/INTEGRATION.md`](docs/INTEGRATION.md) is the three-tier walkthrough; [`docs/BEST_PRACTICES.md`](docs/BEST_PRACTICES.md) covers the discipline rules with citations.

## Documentation

| Doc | Audience | Purpose |
|---|---|---|
| [`docs/INTEGRATION.md`](docs/INTEGRATION.md) | Humans | Three-tier setup walkthrough (architectural reference) |
| [`docs/CLAUDE_MD_SNIPPETS.md`](docs/CLAUDE_MD_SNIPPETS.md) | `/integrate` + humans | Exact marker-bracketed blocks for global + project CLAUDE.md |
| [`docs/BEST_PRACTICES.md`](docs/BEST_PRACTICES.md) | Humans | Discipline rules with research citations (MemGPT, Generative Agents, MCP spec) |
| [`docs/BACKUP_AND_RESTORE.md`](docs/BACKUP_AND_RESTORE.md) | Operators | Volume snapshot, JSON export/import, scheduled, disaster recovery |
| [`docs/AGENT_NOTES.md`](docs/AGENT_NOTES.md) | Future Claude sessions | Gotchas, failure modes, what NOT to do (grep-bait) |
| [`.claude/commands/integrate.md`](.claude/commands/integrate.md) | Claude (via `/integrate`) | Self-contained runbook — state detection, install, verify |
| [`setup/cron/README.md`](setup/cron/README.md) | Operators | Scheduled-backup examples for Linux / macOS / Windows |

## Canned Claude prompt

Paste either prompt into a fresh Claude Code session to bootstrap the integration. The session does **not** need to be inside the johnny-five repo.

**Prompt A — full setup (clone + integrate):**

```text
Set up the johnny-five persistent memory MCP server for my Claude Code environment.

1. Clone https://github.com/coreConvention/johnny-five.git into a directory I'll specify
   (ask me where via AskUserQuestion; default to ~/johnny-five). Skip cloning if it
   already exists at that path.
2. cd into the cloned directory.
3. Read .claude/commands/integrate.md completely before acting. Then execute it
   end to end: state detection → ask scope (global / project / both) → install
   Docker container, hooks, MCP wiring, global CLAUDE.md snippet → verification suite.
4. Use AskUserQuestion at every decision point. Don't skip the verification step.
5. After integration finishes, point me at docs/BEST_PRACTICES.md and
   docs/BACKUP_AND_RESTORE.md for next steps.

Prerequisites I'll have ready: Docker installed and running. If anything else is
missing, tell me what to install — don't try to install system-level tooling yourself.
```

**Prompt B — already cloned, just run /integrate:**

```text
I have the johnny-five repo cloned at <path>. Open it and run the /integrate
slash command. Read .claude/commands/integrate.md fully before acting, ask me
the scope question (global / project / both), then execute end to end with
verification. Use AskUserQuestion for any forks.
```

Either prompt assumes Claude Code is running and Docker is installed. The integration touches `~/.claude/CLAUDE.md`, `~/.claude/settings.json`, `~/.claude/mcp.json`, and `~/.claude/hooks/` — review the diff Claude proposes before approving file writes.

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
      "command": "bash",
      "args": [
        "-c",
        "docker start johnny-five 2>/dev/null || docker run -d --name johnny-five -i -v johnny-five-data:/data -e MEMORY_DB_PATH=/data/memory.db johnny-five:latest >/dev/null; docker attach johnny-five"
      ]
    }
  }
}
```

This reuses an existing `johnny-five` container if one exists, or creates a new one. The container persists between sessions, avoiding cold-start delays from re-downloading the embedding model.

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

## Benchmarks

johnny-five's retrieval pipeline is measured against three published memory benchmarks using the same datasets and methodology mempalace reports their numbers on. Full methodology, per-category breakdowns, and reproducibility commands live in [`benchmarks/BENCHMARKS.md`](benchmarks/BENCHMARKS.md).

| Benchmark | Metric | johnny-five κ=0 | **johnny-five κ=0.30** | mempalace (published) |
|---|---|---|---|---|
| LoCoMo (1986 Qs, session granularity) | R@10 | 60.29% | **85.17%** | 60.3% raw / 88.9% hybrid v5 |
| ConvoMem (~250 items, 5 cats loaded) | Avg recall | 92.87% | **92.93%** | 92.9% |
| MemBench (8500 items, 10 cats, movie) | R@5 | — | **81.82%** | 80.3% |

Headline takeaways:

- **Raw pipelines match exactly.** johnny-five at κ=0 hits mempalace's published raw numbers on both LoCoMo (60.29 vs 60.3) and ConvoMem (92.87 vs 92.9) — validates that our retrieval pipeline, embedding model (`all-MiniLM-L6-v2`), and recall metric are semantically equivalent to their reference.
- **The κ keyword-boost delivers +24.88pp on session-granularity retrieval** (LoCoMo), basically zero on message/turn-granularity (ConvoMem). Makes sense: keyword overlap rescues entity mentions inside multi-turn session documents, adds little when each doc is already one sentence.
- **Beats mempalace on MemBench** by 1.5pp overall (81.82% vs 80.3%), winning on 7 of 10 categories including the hardest (noisy, post_processing, conditional, highlevel_rec).

## johnny-five vs mempalace

Both are open-source, MIT-licensed, local-first memory systems for LLM agents. They solve overlapping problems with different bets. This section is the honest comparison so you can pick the right one for your use case.

### At a glance

| | **johnny-five** | **mempalace** |
|---|---|---|
| Primary target | Claude Code (MCP stdio) | Claude Code, Codex, general LLM agents |
| MCP tool surface | **8 tools** (store / search / recall / update / forget / aging / consolidate / stats) | 29 tools (palace + drawer + wing + room + hall + kg + diary + tunnel operations) |
| Store-call semantics | One `memory_store` with `type`, `tags`, `importance`, `metadata` | Separate `add_drawer` / `diary_write` / `kg_add` per content class |
| Storage format | Digests, typically <500 chars (model-authored) | Verbatim chunks, 800 chars (no paraphrase) |
| Retrieval signals | α·sem + β·rec + γ·freq + δ·imp + κ·lex (5 signals, env-var configurable) | semantic + keyword + temporal + name + quoted-phrase boosts (hybrid v5) |
| Optional LLM rerank | Not implemented (clean extension point in `rerank()`) | Haiku/Sonnet rerank → near-100% on LongMemEval |
| DB engine | SQLite + sqlite-vec + FTS5 (one file, one volume) | ChromaDB |
| Ontology | Flat `project_dir` scoping + free-form tags | Wings / Rooms / Halls / drawers + knowledge-graph nodes |
| Dedup | Automatic at write (cosine-similarity merge) | Explicit `check_duplicate` tool |
| Persistence | Single docker volume (`johnny-five-data`) | ChromaDB store + KG SQLite |
| LLM calls by default | Zero | Zero for core path; optional for rerank/palace |

### Choose johnny-five if …

- You live primarily in Claude Code and want memory to *disappear* into the workflow — small tool surface = small schema overhead in Claude's context window.
- You want short, model-authored memories (corrections, lessons, preferences) rather than archiving full conversation transcripts.
- You care about dedup-on-write because you'll be storing many similar corrections over time.
- You want a one-container deployment (the johnny-five image is the runtime; the named volume is the database).
- You'd rather tune five weights via `MEMORY_*` env vars than navigate a Wing/Room/Hall ontology.

### Choose mempalace if …

- You want the strongest possible retrieval numbers and are willing to pay for an optional LLM rerank (~$0.001/query with Haiku).
- You need verbatim conversation preservation — every word of every session stays searchable, nothing discarded.
- You work across multiple LLM frameworks (Claude Code, Codex, Anthropic SDK, Mastra, etc.) and want one memory server under all of them.
- You want rich cross-domain navigation — tunnels between wings, knowledge-graph relations with validity windows, diary summaries.
- You're comfortable with a larger tool surface (29 tools) and the spatial metaphor (wings/rooms/halls) that mempalace's literature leans on.

### Pros and cons, explicitly

**johnny-five strengths**
- **Low token cost in context.** 8-tool schemas × short descriptions keep Claude's token budget free for the actual work. Mempalace's 29 tools are individually small but stack up in every system prompt.
- **Simple type taxonomy** (`user | feedback | project | reference | lesson`). Claude doesn't have to decide between drawer / diary / kg / wing / room at every write.
- **Automatic dedup at store time.** No separate round-trip tool; near-duplicates merge transparently and bump importance.
- **Env-var weight tuning.** Every scoring knob is a `MEMORY_*` env var — no code changes needed to experiment with signal weights.
- **Benchmark-verified against mempalace's own reference.** Raw pipelines match exactly; +1.5pp over mempalace on MemBench; within 4pp of their hybrid-v5 on LoCoMo with only one of their three boost signals ported.

**johnny-five limitations**
- **No LLM rerank** (yet). Mempalace's +0.6–5pp jumps on LongMemEval/LoCoMo with Haiku rerank are out of reach until someone implements it in `rerank()`.
- **No quoted-phrase or person-name boosts.** These would close most of the ~3.7pp gap to mempalace hybrid v5 on LoCoMo.
- **No verbatim storage.** If you need raw transcripts searchable, johnny-five is the wrong tool — memories are compressed to a digest on write. (Clean extension point: the `summary` column is unused today; a `raw_transcript` column + separate retrieval table would layer on.)
- **No cross-project navigation.** Memories are strictly scoped by `project_dir`; there's no equivalent to mempalace's wing tunnels.
- **No knowledge graph.** Entities and relations live as free-form tags — no validity windows, no explicit timeline, no graph queries.

**mempalace strengths**
- **Highest published retrieval scores.** 96.6% raw / 99.4–100% with rerank on LongMemEval; 88.9% hybrid v5 on LoCoMo without rerank.
- **Verbatim text preserved.** No information ever discarded — you can answer any question about *any* past conversation word, not just what a model decided to remember.
- **Richer architecture surface.** Palace/rooms/wings/tunnels, knowledge graph, diary summaries, agent directories — all addressable as MCP tools.
- **LLM rerank path.** Pay per query for near-perfect retrieval when you need it.
- **Framework-agnostic.** Not tied to Claude Code's MCP stdio — works under Codex, Anthropic SDK directly, local/ollama backends.

**mempalace limitations**
- **Larger cognitive + token footprint.** 29 tools × schemas in Claude's context is a real cost, especially on smaller models; simple use cases pay for features they don't use.
- **Storage grows fast.** Verbatim preservation means one long conversation = many drawers. On a laptop with years of sessions this adds up meaningfully.
- **Ontology tax.** Every write implicitly picks a wing/room/hall; model spends attention on that at every store call.
- **No dedup on write.** Near-identical reflections accumulate unless explicitly pruned; a separate `check_duplicate` tool exists but requires the caller to use it.

### When either is fine

If you're getting started with persistent memory for Claude Code and don't yet know which features matter: **start with johnny-five**, because the minimal surface gets you to "it works" fastest. If you find yourself wanting verbatim recall, LLM rerank, or a knowledge graph, mempalace is the natural next step — and the mental model carries over because both use semantic similarity + keyword boost as their core retrieval.

Neither is the wrong answer. They're products of different hypotheses about what costs most — schema overhead versus information loss — and both are right about their own hypothesis.

## Configuration

All settings are configurable via `MEMORY_*` environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_DB_PATH` | `~/.claude/memory.db` | SQLite database path |
| `MEMORY_MODEL_NAME` | `all-MiniLM-L6-v2` | Embedding model (384-dim) |
| `MEMORY_EMBEDDING_DIM` | `384` | Vector dimensionality (must match model) |
| `MEMORY_TOP_K` | `15` | Default search result count |
| `MEMORY_DEDUP_THRESHOLD` | `0.15` | Cosine distance for near-duplicate detection |
| `MEMORY_ALPHA` | `0.45` | Semantic similarity weight |
| `MEMORY_BETA` | `0.20` | Recency weight |
| `MEMORY_GAMMA` | `0.10` | Frequency weight |
| `MEMORY_DELTA` | `0.25` | Importance weight |
| `MEMORY_KAPPA` | `0.30` | Lexical-overlap (keyword boost) weight |
| `MEMORY_DECAY_RATE` | `0.995` | Daily importance decay multiplier (aging) |
| `MEMORY_RECENCY_DECAY` | `0.01` | Retrieval-time recency decay rate (per-day). `0.01` ≈ 69-day half-life; `0.002` ≈ 1-year half-life; `0.0` disables recency-based decay |
| `MEMORY_HOT_ACCESS_THRESHOLD` | `3` | Min accesses in 30 days to stay in hot tier |
| `MEMORY_WARM_DAYS` | `30` | Days without access before warm demotion |
| `MEMORY_COLD_DAYS` | `180` | Days without access before cold demotion |
| `MEMORY_COLD_IMPORTANCE_THRESHOLD` | `3.0` | Max importance for cold demotion |
| `MEMORY_AUTO_CONSOLIDATE_ENABLED` | `false` | When true, the server runs aging + consolidation automatically on an interval |
| `MEMORY_AUTO_CONSOLIDATE_INTERVAL_HOURS` | `168` | Hours between auto-consolidation runs (default = weekly) |
| `MEMORY_SERVER_PORT` | `8787` | SSE transport port |

### Tuning for long retention (years-scale)

Johnny-five defaults are tuned for months-scale active memory. For multi-year retention, raise the demotion thresholds, slow the decays, and pin anything you never want to lose:

```bash
docker run -d --name johnny-five -i \
  -v johnny-five-data:/data \
  -e MEMORY_RECENCY_DECAY=0.002 \
  -e MEMORY_DECAY_RATE=0.9995 \
  -e MEMORY_WARM_DAYS=180 \
  -e MEMORY_COLD_DAYS=730 \
  -e MEMORY_COLD_IMPORTANCE_THRESHOLD=1.0 \
  -e MEMORY_BETA=0.10 \
  -e MEMORY_KAPPA=0.40 \
  -e MEMORY_AUTO_CONSOLIDATE_ENABLED=true \
  -e MEMORY_AUTO_CONSOLIDATE_INTERVAL_HOURS=168 \
  johnny-five:latest
```

Three complementary mechanisms make long retention work:

- **`recency_decay=0.002`** stretches the retrieval-layer recency half-life from ~69 days to ~347 days, so year-old memories still compete fairly with fresh ones in search results.
- **`warm_days=180, cold_days=730`** keep memories in warm tier for six months instead of one, and cold for two years instead of six months. Together with a lower `cold_importance_threshold=1.0`, only truly low-value stale memories ever get archived.
- **`auto_consolidate_enabled=true`** runs aging + consolidation every 168 h (weekly). Cold-tier clusters get summarised and the originals archived, keeping the DB size manageable even with 5+ years of accumulated memories.

### The `forever-keep` tag (pinning)

Add `forever-keep` to a memory's `tags` list and it becomes immortal:

- Importance decay skips it (tier + score preserved across aging cycles).
- Tier transitions skip it (never demoted to warm/cold/archived; also never auto-promoted to hot — it stays wherever you put it).
- Consolidation skips it (never merged into a summary, never archived as an isolated low-value memory).

Use it for core user preferences, critical workflow rules, or any knowledge the memory system must not discard regardless of access patterns.

```json
{
  "content": "User prefers Postgres over Mongo; rationale in RFC-042.",
  "type": "user",
  "tags": ["forever-keep", "preferences", "database"],
  "importance": 9
}
```

The tag is a regular string — no schema changes, no reserved enum. Adding or removing it at any time via `memory_update` changes the memory's pinning status on the next aging cycle.

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

> The steps below are the **Tier 1 (minimal)** path — prompt-based hooks that *nudge* Claude to use memory. For **Tier 2 (enforced compaction survival)** and **Tier 3 (self-improvement loop hooks)** — including ready-to-copy scripts for command-type `PreCompact` + `SessionStart` + `UserPromptSubmit` + `PostToolUse` hooks — see **[docs/INTEGRATION.md](docs/INTEGRATION.md)**.

### 1. Add to your CLAUDE.md

> **For the full discipline ruleset** (search-first loop, scoping invariants, importance scoring conventions, failure modes), use the comprehensive snippet in **[`docs/CLAUDE_MD_SNIPPETS.md`](docs/CLAUDE_MD_SNIPPETS.md)**. The `setup/CLAUDE.md.snippet` file referenced below is the lighter Tier-1 version — fine to start, but `CLAUDE_MD_SNIPPETS.md` is what makes johnny-five actually useful in practice.

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

## Concurrent Sessions

The default `.mcp.json` config uses `docker attach`, which connects to the container's single main process. This means **only one Claude Code session can connect at a time**.

For concurrent sessions (e.g., parallel worktrees), use SSE transport instead:

```json
{
  "mcpServers": {
    "johnny-five": {
      "type": "sse",
      "url": "http://localhost:8787/sse"
    }
  }
}
```

Run the container with port exposed:

```bash
docker run -d --name johnny-five \
  -v johnny-five-data:/data \
  -p 8787:8787 \
  johnny-five:latest --transport sse --port 8787
```

## Backup & Restore

> The full operations runbook — including JSON export/import, scheduled backups, encryption, and disaster recovery — lives in **[`docs/BACKUP_AND_RESTORE.md`](docs/BACKUP_AND_RESTORE.md)**. Scheduled-backup examples for cron / systemd / launchd / Task Scheduler: **[`setup/cron/README.md`](setup/cron/README.md)**. The recipes below are the quick-reference subset.

The SQLite database lives in a Docker named volume (`johnny-five-data`).

### Create a backup

Quickest path — use the bundled script:

```bash
./setup/scripts/backup-volume.sh                   # writes to ./j5-backups/
./setup/scripts/backup-volume.sh ~/j5-backups      # custom target
```

Or manually:

```bash
docker run --rm -v johnny-five-data:/data -v "$(pwd)":/backup alpine \
  cp /data/memory.db "/backup/johnny-five-backup-$(date +%Y%m%d).db"
```

Or from a running container:

```bash
docker exec johnny-five sqlite3 /data/memory.db ".backup '/data/backup.db'"
docker cp johnny-five:/data/backup.db ./johnny-five-backup.db
docker exec johnny-five rm /data/backup.db
```

### Restore from backup

```bash
docker stop johnny-five
docker run --rm -v johnny-five-data:/data -v "$(pwd)":/backup alpine \
  cp /backup/johnny-five-backup.db /data/memory.db
docker start johnny-five
```

### Migrate to a new machine

1. Create a backup (above)
2. On the new machine: `docker build -t johnny-five:latest .`
3. `docker volume create johnny-five-data`
4. Restore the backup into the new volume
5. Configure `.mcp.json` and hooks

## Troubleshooting

### MCP tools not available / "server not connected"

- Verify Docker is running: `docker info`
- Check container status: `docker ps -a --filter name=johnny-five`
- Check logs: `docker logs johnny-five --tail 20`
- If the container is stuck: `docker rm -f johnny-five` then restart Claude Code

### Docker not running when Claude Code starts

MCP connections are established at session init. If Docker isn't running at that point, johnny-five silently fails to connect and never retries. **Start Docker before starting Claude Code.**

### Container exits immediately

The entrypoint expects stdio input (MCP protocol). The `-i` (interactive) flag is required:

```bash
docker run -d --name johnny-five -i ...  # -i is mandatory
```

### Multiple sessions fail to connect

Only one session can `docker attach` at a time. Switch to [SSE transport](#concurrent-sessions) for concurrent access.

### Windows Git Bash: path mangling

Git Bash translates `/data/...` to `C:/Program Files/Git/data/...` in `docker exec` commands. Use double-slash (`//data/`) when running manual commands:

```bash
docker exec johnny-five ls -la //data/memory.db
```

The `.mcp.json` config is unaffected because it runs inside the container's shell.

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
