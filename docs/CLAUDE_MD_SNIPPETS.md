# CLAUDE.md Snippets for johnny-five

This file is the **single source of truth** for the text blocks that get appended to user CLAUDE.md files during integration. The `/integrate` command reads this file and copies blocks verbatim — between the marker comments — into the user's `~/.claude/CLAUDE.md` and (optionally) per-project `CLAUDE.md`.

If you're a human reading this: skip to the "Why each rule" section at the bottom for explanations. Skip the marker-bracketed blocks unless you're hand-installing.

If you're Claude executing `/integrate`: copy the blocks between the BEGIN/END markers. Do not paraphrase. Do not omit lines. The marker comments must be appended too — they're how re-runs detect prior installs.

---

## Block 1: Global snippet (`~/.claude/CLAUDE.md`)

This is the heavy block. It teaches Claude the read → learn → store discipline that makes johnny-five useful. Append once to the user's global CLAUDE.md.

```markdown
<!-- BEGIN johnny-five (v1) -->
## Johnny-Five Memory System

You have access to a persistent, semantically-searchable memory system via the `johnny-five` MCP server. It stores knowledge across sessions with hybrid retrieval (semantic + lexical + recency + importance), automatic deduplication, and tier-based aging.

**The container must be running for tools to be available.** If `memory_*` tools fail with "server not connected", the container isn't up — see Docker Management below.

### The read-write loop (read more than you write)

The loop is **read → learn → store**, not just store. johnny-five is useless if you only write to it. Searching is the primary value; storing without searching is journaling.

- **SEARCH FIRST**: Before any non-trivial investigation, debugging, or fix attempt — call `memory_search` with a description of the problem area. If a relevant memory exists, USE IT instead of rediscovering the answer.
- **SEARCH ON CORRECTION**: When the user course-corrects you, redirects your approach, or says you're wrong — IMMEDIATELY `memory_search` for related context. Check: did you already have this knowledge and fail to look it up? If yes, that's a compounding failure worth noting.
- **SEARCH ON QUESTIONS**: When the user asks about the project, architecture, or a past decision — `memory_search` before answering. johnny-five may have the answer from a previous session.
- **SEARCH BEFORE STORING**: Before calling `memory_store`, search first. If the lesson already exists, `memory_update` it with new context instead of creating a duplicate.

### When to store (store immediately — don't batch)

Store as soon as the signal arrives. Always include `project_dir` for project-specific memories and relevant `tags` for retrieval.

| Trigger | Type | Importance | Notes |
|---|---|---|---|
| User correction or pushback | `feedback` | 8–9 | High-value signal. Quote the user's correction in `content`. |
| User preference or working style | `user` | 7–8 | Things that change how you should respond, not what you respond about. |
| Project decision, architecture, convention | `project` | 6–8 | Use 8 for load-bearing decisions, 6 for incidental notes. |
| Debugging gotcha, mistake, postmortem | `lesson` | 7–9 | Format: situation → mistake → correction → rule. |
| External resource pointer | `reference` | 5–7 | Dashboard URLs, ticket systems, on-call rotations. |
| Cross-cutting insight ("★ box" pattern) | `lesson` or `project` | 7–8 | Non-obvious connections between systems are high-value cross-session context. |

### What NOT to store

- Code patterns derivable from reading the current repo
- Git history / who-changed-what (use `git log`/`git blame`)
- Ephemeral task state (use TodoWrite, not memory)
- Anything already documented in CLAUDE.md
- Debugging fix recipes when the fix is already in the commit

### Importance scoring (0–10)

| Range | Meaning |
|---|---|
| 0–3 | Trivial, decay quickly. Almost never use this range. |
| 4–6 | Useful context, fine to age out over months. |
| 7–8 | Important — actively wanted on next session start. |
| 9 | Critical — user explicitly emphasised, or load-bearing for correct behavior. |
| 10 | Reserve for rich session-state stored before compaction. |

### The `forever-keep` tag

Add `"forever-keep"` to a memory's `tags` to pin it permanently:

- Importance never decays
- Tier never demotes (stays where you put it)
- Consolidation skips it

Use **only** for: core user preferences that define your behavior, critical workflow invariants, expensive-to-rediscover gotchas, reference pointers the user must never lose. Treat it as the "break glass" tag — overuse defeats the aging system.

### Project scoping (LOAD-BEARING — do not get this wrong)

- Memories are scoped per-project via the `project_dir` parameter.
- **Never hardcode a project path.** Use `$CLAUDE_PROJECT_DIR` (in shell) or current working directory. Loading the wrong project's memories silently pollutes your context.
- The `SessionStart` hook auto-recalls memories scoped to the current project. You see them as `# Resume Context`. **Don't call `memory_recall` again unless you need a DIFFERENT project's memories** — prefer `memory_search` for targeted queries within the current project.

### Session continuity (compaction survival)

Compaction strips prompt-specific instructions. The `precompact-enforce` command hook writes a mechanical floor (branch, cwd, git status) if you don't store anything richer first. **Your model-authored summary is always richer than mechanical-floor.** Before compaction, if you have non-obvious context (subtle decisions, active blockers, runtime ports, open plan files), call `memory_store` with `type='project'`, `tags=['session-state', 'precompact']`, importance 9–10.

### Maintenance

- Call `memory_aging` periodically (once per long session) to decay stale memories.
- Call `memory_consolidate` if `memory_stats` shows >30% cold-tier — it clusters cold memories and summarizes them.
- `memory_stats` for occasional health checks.

### Docker management

The johnny-five container must be running before Claude Code starts (MCP connections are established at session init; if Docker isn't up, the connection silently fails and never retries).

```bash
# Check container status
docker ps --filter name=johnny-five --format "{{.Names}} {{.Status}}"

# Start a stopped container
docker start johnny-five

# Recreate from image (preserves data via named volume)
docker stop johnny-five 2>/dev/null; docker rm johnny-five 2>/dev/null
docker run -d --name johnny-five -i -v johnny-five-data:/data johnny-five:latest --transport stdio
```

If MCP tools become unavailable mid-session: restart the Claude Code session after starting Docker. Mid-session reconnection is not retried.

### Failure modes (quick reference)

| Symptom | Cause | Fix |
|---|---|---|
| `memory_*` tools missing | Container not running at session start | Start Docker, restart Claude Code session |
| `memory_search` returns nothing | Wrong `project_dir`, or fresh DB | Verify cwd; check `memory_stats` |
| Resume context shows "mechanical-floor" tag | Last session didn't store rich state | Inspect `git status`/`git log`; ask user if needed |
| Same lesson stored twice | Skipped search-before-store | `memory_update` to merge; tighten the loop next time |

For deeper integration (export/import, backup, scheduled maintenance) see the johnny-five repo's `docs/BACKUP_AND_RESTORE.md`.
<!-- END johnny-five -->
```

---

## Block 2: Project snippet (`<project>/CLAUDE.md`)

Smaller. Drops into a per-project CLAUDE.md to remind Claude that THIS project is wired up and to suggest project-specific memory conventions. Optional — global wiring alone is sufficient for J5 to work.

```markdown
<!-- BEGIN johnny-five-project (v1) -->
## Memory (johnny-five)

This project is wired into the johnny-five memory system. The global discipline rules in `~/.claude/CLAUDE.md` apply. Project-specific notes:

- `project_dir` for this project: use `$CLAUDE_PROJECT_DIR` or the current cwd; do not hardcode a path that could break across machines.
- Lesson tags worth using here: `<fill in: e.g. ravendb, multi-tenant, graphql, deployment>`. Add tags as patterns emerge; don't over-engineer the taxonomy upfront.
- File-based backup of lessons (optional): `.claude/memory/lessons.md` — append a structured `[Category] Description → Mistake → Correction → Rule` entry whenever you `memory_store` a lesson, so the file is grep-able even when the container is down.
<!-- END johnny-five-project -->
```

---

## Block 3: Hooks settings additions

These go into `~/.claude/settings.json` (or per-project `.claude/settings.json`). The `/integrate` command merges the `hooks` key with anything already there. Marker comments don't work in JSON; the integrate command instead treats this entire `hooks` object as the desired state and reconciles it.

The canonical hooks JSON lives in **`setup/hooks.json.enforced.snippet`** at the J5 repo root. That file is the source the integrate command reads. Don't edit a copy of the JSON in this doc — edit the snippet file and let the integrate command pick it up.

---

## Why each rule (for humans)

**Read → learn → store** — Storing without searching is journaling: it costs DB space and conveys nothing back to future-you. Search-first ensures every store call has been informed by what's already known. Validated externally: "Lost in the Middle" (Liu et al. 2023, [arXiv:2307.03172](https://arxiv.org/abs/2307.03172)) shows that loading too much retrieved content into context degrades model performance — search-first beats full-context loading.

**Search on correction** — The single highest-value signal in the entire system. Most repeat mistakes are mistakes Claude already learned about and failed to look up. Catching that compounding failure is worth more than any individual lesson.

**Search before storing** — Avoids duplicate memories that pollute future searches. Validated by every dedup-aware system in the literature (A-MEM, MemGPT). J5 has dedup on store as a safety net, but searching first is cheaper and more disciplined.

**Project scoping is load-bearing** — Cross-project memory pollution is silent; you don't get an error, you just get bad recommendations from the wrong project's context. Validated by MCP `roots` spec ([modelcontextprotocol.io](https://modelcontextprotocol.io/)) and LangGraph namespacing. Hardcoded paths break when the cwd shifts (worktrees, mounted Docker volumes, CI runners).

**Importance 0–10 with reserved 10 for compaction** — The 1–10 LLM-rated importance scale comes directly from Generative Agents (Park et al. 2023, [arXiv:2304.03442](https://arxiv.org/abs/2304.03442)). Reserving 10 for session-state-before-compaction means those memories never decay out of the always-load tier of `memory_recall`.

**`forever-keep` for break-glass pinning** — The aging system is good at forgetting things you genuinely don't need anymore. It's bad at recognising "this one is structural and must never be forgotten." `forever-keep` is the override. Overuse defeats the aging system; underuse loses critical memories.

**Compaction survival is the killer feature** — The thing other memory systems don't have because they don't have hook integration. The `precompact-enforce` mechanical floor guarantees you never lose more than branch/cwd/git-status; model-authored session-state guarantees you keep the actually-interesting context.

---

## Verifying a snippet was applied

After installation:

```bash
# Should print the BEGIN/END markers
grep -E "BEGIN johnny-five|END johnny-five" ~/.claude/CLAUDE.md

# Should print the version
grep "BEGIN johnny-five (v" ~/.claude/CLAUDE.md
```

If a future J5 release ships a `v2` snippet, the integrate command will detect the `v1` markers, prompt to upgrade, replace the block atomically, and update the version tag.
