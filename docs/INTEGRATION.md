# Johnny-Five Integration Guide

A deeper walkthrough than the README's Quick Start. Covers how to adopt johnny-five progressively — from "works when the model remembers" to "enforced by hooks so it can't forget" — plus tuning, daily operation, and migration.

Audience: engineers running Claude Code locally, on macOS / Linux / Windows (Git Bash), who want Claude to remember things across sessions instead of re-explaining the same context every time.

---

## Table of contents

- [The three tiers of integration](#the-three-tiers-of-integration)
- [Prerequisites](#prerequisites)
- [Tier 1 — minimal (prompt-based, advisory)](#tier-1--minimal-prompt-based-advisory)
- [Tier 2 — enforced compaction survival](#tier-2--enforced-compaction-survival)
- [Tier 3 — self-improvement loop](#tier-3--self-improvement-loop)
- [Configuration tuning](#configuration-tuning)
- [Typical adoption timeline](#typical-adoption-timeline)
- [Daily operation](#daily-operation)
- [Upgrade and migration](#upgrade-and-migration)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)

---

## The three tiers of integration

Johnny-five can be bolted on in stages. Each tier is a strict superset of the one before, so you can start small and level up as you see value.

| Tier | What it gives you | How | Failure mode when the model forgets |
|------|-------------------|-----|--------------------------------------|
| **1. Minimal** | Claude knows the memory tools exist and is *nudged* to use them. | Prompt-type `SessionStart` + `Stop` hooks. CLAUDE.md snippet. | The model skips `memory_recall` because it's distracted; skips `memory_store` because it's about to end turn. Memory still works, just underused. |
| **2. Enforced compaction survival** | Session state *is* saved before compaction and *is* recalled after, whether the model complies or not. | Command-type `PreCompact` + `SessionStart` hooks that reach into johnny-five via `docker exec`. Mechanical-floor fallback. | Never loses more than mechanical context (branch, cwd, recent files). Model-authored is preferred but not required. |
| **3. Self-improvement loop** | Correction signals and repeat tool failures trigger automatic `memory_search` so Claude doesn't rediscover what it already learned. | Command-type `UserPromptSubmit` regex-detector + `PostToolUse` failure-pattern tracker. | Stops the "you already told me this" pattern and the "retry the same broken command three times" pattern. |

You do **not** need to do all three at once. Tier 1 is enough to start seeing value. Tier 2 is recommended once you've hit one painful compaction. Tier 3 is recommended once you've hit the "you already told me this" frustration at least twice.

---

## Prerequisites

- Docker (any recent version; Docker Desktop or native).
- Claude Code installed (`claude` CLI available).
- Python ≥ 3.8 on PATH (used by a couple of the hook scripts for JSON parsing; if you're on a dev machine it's almost certainly there).
- Node ≥ 18 on PATH *if* you enable the Tier 3 `tool-failure-tracker.js` hook.
- Git Bash on Windows (MSYS/MinGW). PowerShell alone won't run the `.sh` hook scripts.

---

## Tier 1 — minimal (prompt-based, advisory)

This is the path the main [README](../README.md) walks through. The short version:

1. **Run the container** once so the named volume `johnny-five-data` exists and the image is cached:
   ```bash
   docker build -t johnny-five:latest .
   docker run -d --name johnny-five -i -v johnny-five-data:/data johnny-five:latest
   ```
2. **Add MCP config** to your project's `.mcp.json` (see README Quick Start).
3. **Copy CLAUDE.md rules**:
   ```bash
   cat setup/CLAUDE.md.snippet >> ~/.claude/CLAUDE.md
   ```
4. **Copy the basic hook config**:
   ```bash
   # if ~/.claude/settings.json doesn't exist
   cp setup/hooks.json.snippet ~/.claude/settings.json

   # if it exists, merge the `hooks` key manually with jq or a text editor
   ```
5. **Verify**: `docker ps --filter name=johnny-five` should show `Up …`. Start a Claude Code session and ask it to run `memory_stats`. You should see `{"by_type": {}, "by_tier": {}, "total": 0}`.

That's it. Memory works. Claude is *told* about it. It'll use memory most of the time and miss sometimes.

**When Tier 1 is enough:** you want to try johnny-five out, have casual Claude Code usage, or your sessions rarely approach the compaction limit.

---

## Tier 2 — enforced compaction survival

### The problem Tier 1 doesn't solve

Claude Code compacts context when it fills up. Compaction strips prompt-specific instructions and collapses earlier turns into a summary. If you were three steps into an orchestrator prompt, post-compaction you have a summary that omits the prompt's labels, gates, and conventions. The prompt-type `PreCompact` hook in Tier 1 *asks* Claude to save state — but if the model is mid-task or misreads the hook, the save doesn't happen and the next session has nothing to resume from.

### The Tier 2 fix

Two command-type hooks bridge into johnny-five via `docker exec` (no new transport required):

- `precompact-enforce.sh` runs on `PreCompact` after the prompt-type hook. It queries johnny-five for a recent `session-state` memory scoped to the current project. If found (model complied), it emits `{"continue": true}`. If not, it *writes a mechanical floor* itself — branch name, cwd, `git status --porcelain`, session id — with tags `[session-state, precompact, mechanical-floor]`, importance 7. Compaction always proceeds; there is always something to resume from.
- `session-start-recall.sh` runs on `SessionStart`. It calls `memory_recall` on johnny-five scoped to `$CLAUDE_PROJECT_DIR`, formats the top results as a Markdown `# Resume Context` block, and emits as `hookSpecificOutput.additionalContext`. Claude sees it automatically; no tool call needed.

### Installation

1. **Copy the scripts** into your global hooks directory:
   ```bash
   mkdir -p ~/.claude/hooks
   cp setup/hooks/precompact-enforce.sh ~/.claude/hooks/
   cp setup/hooks/session-start-recall.sh ~/.claude/hooks/
   chmod +x ~/.claude/hooks/*.sh
   ```
2. **Replace (or merge)** your `~/.claude/settings.json` hooks with [`setup/hooks.json.enforced.snippet`](../setup/hooks.json.enforced.snippet). The key additions:
   - `SessionStart` command hook points to `session-start-recall.sh`.
   - `PreCompact` is a two-hook chain: existing prompt + new command.
3. **Verify** by manually firing each hook:
   ```bash
   CLAUDE_PROJECT_DIR=$(pwd) bash ~/.claude/hooks/session-start-recall.sh | head -c 500
   # Expect: JSON with hookSpecificOutput.additionalContext

   CLAUDE_PROJECT_DIR=$(pwd) bash ~/.claude/hooks/precompact-enforce.sh
   # Expect: JSON with "continue": true
   ```

### What you get

- No more "the previous session compacted and I lost my context" — the resume block is always injected.
- No more wondering if the model remembered to save — the mechanical floor is a safety net.
- Zero new johnny-five server dependencies. `docker exec` reaches into the running container and calls the MCP tool functions directly.

### Container naming

The scripts assume your container is literally named `johnny-five`. If you use a different name, edit the `docker exec johnny-five …` lines. One sed line in each script.

---

## Tier 3 — self-improvement loop

### The problems Tier 3 solves

- **"I already told you this."** You correct Claude. It apologises. Same mistake next session. Root cause: the correction wasn't stored, OR was stored but never surfaced when relevant.
- **"Why did you run that failing command three times?"** A tool fails with the same error twice. Claude retries a third time with minor variations. Time wasted on exhausted approaches rather than root-cause diagnosis.

### The Tier 3 fix

Two more command-type hooks:

- `user-prompt-correction.sh` (UserPromptSubmit) regex-matches the user's message for correction signals (`actually`, `wrong`, `incorrect`, `already told`, `you forgot`, `that's not right`, `course-correct`, `no, that/you`). On a match, it runs `memory_search` with the message as query, `token_budget=600`, top-3, and injects the results as `additionalContext`. Claude sees "possibly-relevant prior lessons" before responding to the correction.
- `tool-failure-tracker.js` (PostToolUse) hashes `(tool_name, tool_input)` per call. After the third failure of the same signature in a session, injects an advisory: "before retrying, consider `memory_search` for prior workarounds." Session state persists to `~/.claude/hooks/state/tool-failures-<session>.json`; files older than 7 days are cleaned up automatically.

### Installation

1. **Copy the scripts**:
   ```bash
   cp setup/hooks/user-prompt-correction.sh ~/.claude/hooks/
   cp setup/hooks/tool-failure-tracker.js ~/.claude/hooks/
   chmod +x ~/.claude/hooks/user-prompt-correction.sh
   ```
2. **Extend** `~/.claude/settings.json` with the `UserPromptSubmit` and `PostToolUse` entries from [`setup/hooks.json.enforced.snippet`](../setup/hooks.json.enforced.snippet). If you already have other `PostToolUse` hooks (e.g. a context-monitor), **add** the tool-failure-tracker to the array rather than replacing.
3. **Verify** with synthetic payloads:
   ```bash
   # positive correction-signal test
   echo '{"session_id":"test","cwd":"'"$(pwd)"'","prompt":"actually you already told me about X"}' \
     | bash ~/.claude/hooks/user-prompt-correction.sh
   # expect: JSON with additionalContext

   # negative (no correction signal)
   echo '{"session_id":"test","cwd":"'"$(pwd)"'","prompt":"hello"}' \
     | bash ~/.claude/hooks/user-prompt-correction.sh
   # expect: empty output

   # failure-tracker: run three times with the same tool_input
   for i in 1 2 3; do
     echo '{"session_id":"t","tool_name":"Bash","tool_input":{"command":"ls /nope"},"tool_response":{"error":"nope"}}' \
       | node ~/.claude/hooks/tool-failure-tracker.js
     echo ""
   done
   # expect: silent, silent, JSON advisory on the third
   ```

### A note on the Stop-hook reflection pattern

The plan doc shipped with this repo originally included a prompt-type `Stop` hook that asks Claude to reflect on lessons learned before ending. **We deliberately don't include one in the enforced snippet.** Prompt-type Stop hooks can't reach MCP tools — MCP servers are shut down at Stop time — so a prompt like "now call `memory_store`" would fail. The existing CLAUDE.md snippet's guidance ("store immediately when you learn something") covers the same ground without the hook plumbing. If you really want reflection-at-Stop, implement it as a command-type hook that calls `docker exec` and manages session-scoped state to avoid infinite Stop-block loops.

---

## Configuration tuning

All scoring weights are `MEMORY_*` env vars. The defaults below are sensible for ~100–1000 memories in a multi-project developer workflow.

| Variable | Default | What it controls | When to tune |
|---|---|---|---|
| `MEMORY_ALPHA` | `0.45` | Semantic similarity weight | Rarely. This is the primary signal. |
| `MEMORY_BETA` | `0.20` | Recency | Lower (0.10) if you want older stable knowledge to rank with newer corrections. |
| `MEMORY_GAMMA` | `0.10` | Frequency (access count) | Rarely. |
| `MEMORY_DELTA` | `0.25` | Importance | Higher (0.35) if you're diligent about setting importance correctly. |
| `MEMORY_KAPPA` | `0.30` | Lexical overlap (keyword boost) | Higher (0.40–0.50) for entity-heavy queries ("what's the name of that service?"). Lower (0.15) if you see too many false-keyword-match surface. Set to 0 to disable. |
| `MEMORY_RECENCY_DECAY` | `0.01` | Retrieval-time recency half-life. Default `0.01` ≈ 69-day half-life. | `0.002` (~1-year half-life) for long retention. `0.0` disables recency decay entirely. |
| `MEMORY_DECAY_RATE` | `0.995` | Daily importance-decay multiplier (aging cycle, not retrieval) | `0.9995` for slow years-scale decay. `1.0` disables. |
| `MEMORY_DEDUP_THRESHOLD` | `0.15` | Cosine distance under which a new memory merges into a duplicate | Lower (0.08) for aggressive dedup; higher (0.25) to keep near-duplicates separate. |
| `MEMORY_WARM_DAYS` | `30` | Days of inactivity before a memory demotes to warm | Shorter (14) if your DB grows fast. `180` for multi-month active window. |
| `MEMORY_COLD_DAYS` | `180` | Warm → cold | Shorter (90) for aggressive consolidation. `730` for multi-year retention. |
| `MEMORY_COLD_IMPORTANCE_THRESHOLD` | `3.0` | Max importance for cold demotion | Lower (`1.0`) to protect more memories from cold. |
| `MEMORY_AUTO_CONSOLIDATE_ENABLED` | `false` | When true, server runs aging + consolidation automatically on an interval | `true` to remove the manual cron. |
| `MEMORY_AUTO_CONSOLIDATE_INTERVAL_HOURS` | `168` | Hours between auto-consolidation cycles | `168` = weekly (recommended). Floor enforced at 60 s. |

Override any of them at container run time:
```bash
docker run -d --name johnny-five -i \
  -v johnny-five-data:/data \
  -e MEMORY_KAPPA=0.40 \
  -e MEMORY_DEDUP_THRESHOLD=0.10 \
  johnny-five:latest
```

### `token_budget` on recall/search

Added in the hybrid-retrieval release. Use it any time you're injecting results into hook context — keeps the hook output from ballooning:
```python
await tool_memory_recall(
    project_dir=cwd,
    initial_context="session start",
    top_k=15,
    token_budget=1500,  # cumulative cap on returned content
)
```
Top-1 is always included even if it alone exceeds the budget (otherwise "did any memory match?" becomes ambiguous). Truncation happens at the first result that would exceed. Tiktoken-based estimation with a `len/4` fallback when tiktoken isn't installed.

### Long retention (years-scale)

Johnny-five's defaults are tuned for months-scale active memory. For multi-year retention, combine four knobs and enable auto-consolidation:

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

Why each knob matters for long retention:

- **`MEMORY_RECENCY_DECAY=0.002`** stretches the retrieval-layer recency half-life from ~69 days to ~347 days. Year-old memories compete fairly with fresh ones in search results.
- **`MEMORY_DECAY_RATE=0.9995`** slows per-day importance decay from ~0.5% to ~0.05%. A memory stored at importance 8 takes ~4 years to decay below 3.0 rather than ~4 months.
- **`MEMORY_WARM_DAYS=180` + `MEMORY_COLD_DAYS=730`** extends the active-memory window to 6 months and cold demotion to 2 years.
- **`MEMORY_COLD_IMPORTANCE_THRESHOLD=1.0`** protects everything above very-low importance from cold demotion.
- **`MEMORY_BETA=0.10`** (from 0.20) lowers the retrieval weight on recency so older stable knowledge ranks alongside new corrections instead of always behind.
- **`MEMORY_KAPPA=0.40`** (from 0.30) bumps keyword-overlap weight — with more memories in the pool, literal keyword match becomes a more valuable rescue signal against semantic-neighbour noise.
- **Auto-consolidation weekly** keeps the cold-tier cluster count manageable without requiring an external cron.

### The `forever-keep` tag (pinning)

Add `forever-keep` to a memory's `tags` list and it becomes immortal:

- **Importance decay skips it** — importance is preserved across every aging cycle, no matter how many days without access.
- **Tier transitions skip it** — a pinned memory never demotes to warm/cold/archived and is never auto-promoted to hot either. It stays in whatever tier you put it in (usually `hot`).
- **Consolidation skips it** — never merged into a summary, never archived as an "isolated low-value" memory.

Store example:

```json
{
  "content": "User prefers Postgres over Mongo. Rationale: we lost six weeks to Mongo's lack of transactions on the old payments service. Never again.",
  "type": "user",
  "tags": ["forever-keep", "preferences", "database", "postmortem"],
  "importance": 9
}
```

`forever-keep` is a regular string tag — no schema changes, no reserved enum, no special tool. Add or remove it at any time via `memory_update`; the change takes effect on the next aging cycle. Use it sparingly for:

- Core user preferences that define how the assistant should behave
- Critical workflow rules or invariants (security rules, architectural constraints)
- Expensive-to-rediscover gotchas (postmortems, subtle bugs)
- Reference pointers the system must never forget (production URLs, ticket systems, dashboards)

Don't use it as a substitute for high importance on every memory — that would defeat the whole aging system. Treat it as the "break glass" tag for the tiny fraction of memories that are load-bearing for correct behaviour.

### Auto-consolidation scheduler

With `MEMORY_AUTO_CONSOLIDATE_ENABLED=true` the MCP server spawns a background asyncio task on startup that runs:

```
while alive:
    sleep(interval_hours * 3600)
    tool_memory_aging()       # decay + tier transitions
    tool_memory_consolidate() # cluster cold, summarise, archive originals
```

Logs to **stderr** (not stdout — stdout is the MCP protocol channel on stdio transport). Errors are caught and reported; the loop keeps running. Cancellation during shutdown is handled cleanly via asyncio's `CancelledError` protocol.

The task uses its own per-iteration DB connection via the existing `tool_memory_aging` / `tool_memory_consolidate` handlers; SQLite WAL mode handles the occasional concurrent-writer case gracefully. In practice, consolidation runs for 1–10 seconds once a week, so collision with a user memory_store call is vanishingly rare.

When to disable auto-consolidation:
- You prefer explicit, visible runs — call `memory_consolidate` manually whenever the DB gets large.
- Your deployment runs under a container orchestrator that restarts frequently enough that the background loop rarely hits its interval.
- You want predictable cost — auto-consolidation's embedding compute runs on your CPU at weekly intervals regardless of activity.

---

## Typical adoption timeline

Rough guide — adjust to taste.

- **Day 1**: Tier 1. Run the container, copy CLAUDE.md snippet, add basic hook snippet, add `.mcp.json`. Use it casually. Ask Claude to remember things as they come up.
- **Week 1**: Your memory DB has 20–50 entries. You've seen Claude recall something useful at least once. You've also seen it miss at least once. Move to Tier 2.
- **Week 2**: You've had a session compact at least once. The Tier 2 `session-start-recall.sh` hook has proven itself by surfacing a resume context from a mechanical floor. You trust it.
- **Week 3–4**: You've hit the "you already told me" frustration twice. Enable Tier 3.
- **Month 2**: You notice your DB has 300+ memories, a few hundred lessons. Run `memory_consolidate` once to cluster old cold memories. Consider tuning `MEMORY_WARM_DAYS` down.
- **Month 3+**: Maintenance. Backup periodically. Rebuild image on each johnny-five release.

---

## Daily operation

### What Claude sees at session start

With Tier 2 enabled, the first thing in Claude's context is the `# Resume Context` block from `session-start-recall.sh`. Example (abbreviated):
```markdown
# Resume Context (auto-recalled by session-start-recall hook)

Scoped to `project_dir=Z:/Personal/your-app`. 12 memories loaded.

## Last session-state (2026-04-20 23:16 UTC, importance 9.0)
```
{"prompt_file": "...", "branch": "feat/X", "current_step": "...", ...}
```

## Top lessons for this project
- Use `Pascal_Case` for database column names; we had a migration fail on this.
- The dev server needs `FOO=bar` in env; it's not in the dotenv sample.

## User preferences / feedback
- Brief output preferred; no restatement of what just happened.
```
Claude reads this and can continue the task without asking what's going on.

### When to `memory_store` manually

Beyond what the CLAUDE.md rules say, the practical triggers are:
- You correct Claude. Claude's response should include a `memory_store` call before moving on. If it doesn't, the Tier 3 `user-prompt-correction.sh` hook will at least search for related prior lessons next time, but you've still lost the new lesson — push Claude to store it explicitly.
- You make an architectural decision ("we're going to use Postgres instead of Mongo"). One `memory_store` with `type: project`, importance 8+.
- You learn a gotcha ("the health check returns 200 even when the DB is down"). `memory_store` with `type: lesson`, importance 8+.

### Storage hygiene

Once a month, eyeball:
```bash
docker exec johnny-five python -c "
import asyncio
from claude_memory.mcp.tools import tool_memory_stats
print(asyncio.run(tool_memory_stats()))
"
```
If cold-tier count is > 30% of total, run `memory_consolidate`. If total is > 2000 and recall feels slow, consider archiving old projects' memories.

---

## Upgrade and migration

### Upgrading the image

Johnny-five is actively developed. When a new release lands:

1. **Back up the DB first** (always):
   ```bash
   mkdir -p ~/johnny-five-backups
   docker exec -i johnny-five python -c "
   import sqlite3
   src = sqlite3.connect('/data/memory.db')
   dst = sqlite3.connect('/tmp/backup.db')
   src.backup(dst)
   "
   docker cp johnny-five:/tmp/backup.db ~/johnny-five-backups/memory-$(date +%Y%m%d).db
   ```
2. **Tag the current image** as a rollback target:
   ```bash
   docker tag johnny-five:latest johnny-five:prev
   ```
3. **Rebuild**:
   ```bash
   cd /path/to/johnny-five && git pull && docker build -t johnny-five:latest .
   ```
4. **Recreate the container**:
   ```bash
   docker stop johnny-five && docker rm johnny-five
   docker run -d --name johnny-five -i -v johnny-five-data:/data johnny-five:latest
   ```
   The named volume `johnny-five-data` persists across `docker rm`, so your DB is untouched.
5. **Verify**: run `memory_stats` — the count should match pre-upgrade.
6. **Rollback if anything broke**:
   ```bash
   docker stop johnny-five && docker rm johnny-five
   docker run -d --name johnny-five -i -v johnny-five-data:/data johnny-five:prev
   ```

### Moving to a new machine

See README's "Migrate to a new machine" section. The short version: backup DB, ship backup and repo, rebuild image on target, restore backup into the new volume, configure `.mcp.json` and hooks.

---

## Troubleshooting

### "No memories loaded" at SessionStart

Hook output says the container is reachable but `memory_recall` returned empty for your `project_dir`. Most likely you haven't stored anything scoped to this directory yet. Check by running `memory_stats` — if the total is 0, that's expected for a fresh DB; if non-zero, check that you're in the directory you think you are and that your memories were stored with the matching `project_dir` (hardcoded paths are a common bug — use `$CLAUDE_PROJECT_DIR` or the current cwd).

### `precompact-enforce.sh` always writes a mechanical floor

This means the command-type hook fires before the prompt-type hook's response is captured. That's a Claude Code event-ordering detail and out of johnny-five's control. The mechanical floor is harmless — it just means model-authored summaries are rare until/unless the event order flips. If you find the mechanical floor insufficient, manually call `memory_store` with tags `[session-state, precompact]` a few seconds before you expect compaction.

### Hook scripts run slowly (~10s the first time)

Cold-start: sentence-transformers lazy-loads the 384-dim model on the first `memory_recall` after the container starts. Subsequent calls are <1s. If the container is frequently recreated (e.g. in CI), pre-download the model by running `docker exec johnny-five python -c "from claude_memory.embeddings.encoder import get_encoder; get_encoder('all-MiniLM-L6-v2').encode('warmup')"` at container start.

### "Module 'vec0' not found" when inspecting the DB with `sqlite3`

The `memories_vec` table uses the sqlite-vec extension. Plain `sqlite3` without the extension loaded can't query it. The `memories` table (the one you actually care about) is a regular SQLite table and queryable without the extension. Or run queries inside the container where the extension is already loaded.

### UserPromptSubmit hook triggers on messages that aren't really corrections

The regex is deliberately conservative — a false positive costs one cheap `memory_search`. If you want to tighten it, edit the regex in `~/.claude/hooks/user-prompt-correction.sh`. Suggested stricter patterns: require an exclamation, require the correction signal at the start of the message, or require multi-word signals only.

### Container name conflict after recreate

```
docker: Error response from daemon: Conflict. The container name "/johnny-five" is already in use
```
Run `docker rm johnny-five` first (the container was stopped but not removed). If even that fails, `docker rm -f johnny-five`.

---

## FAQ

**Does the hybrid keyword-boost signal (κ) cost me anything?**
Negligible. It runs in-memory against already-retrieved candidates, no new DB queries. Expect <5ms overhead per search for typical memory sizes.

**Can I run multiple projects' memories in one johnny-five instance?**
Yes. Every store/search/recall accepts `project_dir` as a scope. As long as callers pass the right value (which the hooks enforce via `$CLAUDE_PROJECT_DIR`), memories stay isolated. No need for one container per project.

**Does Claude Code auto-restart johnny-five if the container dies?**
Only if your `.mcp.json` entry uses the `docker start || docker run` pattern (see README Quick Start). Raw `docker attach` won't restart a stopped container.

**Why stdio instead of HTTP for the MCP transport?**
stdio is the default MCP transport in Claude Code and has zero network surface. HTTP (SSE) is supported via `--transport sse` but requires port-forwarding. The hook scripts in this guide use `docker exec` which works regardless of transport choice.

**My DB is at 5000 memories — is that too many?**
No. SQLite + the FTS5 + vec indexes handle hundreds of thousands of rows without issue. What can get slow is the model cold-start and per-call overhead from MCP marshaling. If *recall* feels slow, tune `MEMORY_WARM_DAYS` / `MEMORY_COLD_DAYS` down so the tier filters exclude more candidates.

**What happens if I lose `~/.claude/memory.db` or the named volume?**
If you had a backup, restore it. If not, you start fresh. Johnny-five intentionally has no cloud sync — if that's a requirement, add it yourself via a scheduled `sqlite3 .backup` into a dropbox/S3-synced directory.

**Can johnny-five call an LLM to rerank results?**
Not currently. The retrieval pipeline is: embed → vector search + FTS5 search → multi-signal score + keyword boost → return. There's no LLM in the loop. If you want LLM-rerank (à la mempalace's optional reranker), the `rerank` function in `src/claude_memory/retrieval/reranker.py` is the right extension point.
