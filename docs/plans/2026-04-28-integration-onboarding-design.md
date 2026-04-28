# Integration & Onboarding Design — johnny-five

**Date:** 2026-04-28
**Status:** Approved, implementing
**Branch:** `feat/onboarding-integration`

## Goal

Make it possible for a user to point Claude at this repo and get the same johnny-five integration Brandon uses, without conversation-context babysitting. Documentation is **agent-first**: the primary readers are future Claude sessions executing `/integrate`, not humans skimming for marketing.

## Why this is needed

The repo today has the engine (8 MCP tools, retrieval, lifecycle), `setup/hooks/`, `docs/INTEGRATION.md` (deep architectural reference), and a tier-1 CLAUDE.md snippet. What's missing for cold integration:

1. A self-contained agent runbook that handles state detection, idempotent installs, and verification end-to-end.
2. The full discipline ruleset (search-first, store-on-correction, scoping invariants) — currently lives only in Brandon's private `~/.claude/CLAUDE.md` and is what makes J5 *useful* rather than just installed.
3. Backup/restore as first-class operations docs (the README has a paragraph; ops needs a runbook).
4. A Claude-facing gotcha file Claude will grep when J5 misbehaves.

## Audience split

| Reader | Reads | Doesn't read |
|---|---|---|
| **Claude executing `/integrate`** | `.claude/commands/integrate.md`, `docs/CLAUDE_MD_SNIPPETS.md`, `docs/AGENT_NOTES.md` | best-practices prose, backup ops |
| **Human first-time user** | README, `docs/INTEGRATION.md`, `docs/BEST_PRACTICES.md` | runbook internals |
| **Operator (cron, restore)** | `docs/BACKUP_AND_RESTORE.md`, `setup/cron/README.md`, `setup/scripts/*` | discipline rules |

## File plan

```
docs/
  INTEGRATION.md          KEEP — existing tier 1/2/3 architectural doc
  CLAUDE_MD_SNIPPETS.md   NEW — exact marker-bracketed blocks for global + project
                                 CLAUDE.md. Single source of truth.
  BEST_PRACTICES.md       NEW — discipline rules with research citations.
                                 Human-facing primer.
  BACKUP_AND_RESTORE.md   NEW — three-mode runbook (volume snapshot, export/import,
                                 scheduled). Disaster recovery section.
  AGENT_NOTES.md          NEW — Claude-facing gotchas. Pitfalls, what to do when
                                 things break, what NOT to do. Filename keyword-
                                 grep-discoverable.
  plans/2026-04-28-integration-onboarding-design.md  THIS FILE

.claude/commands/
  integrate.md            NEW — agent runbook. State detection → AskUserQuestion
                                 forks → idempotent global wiring → idempotent
                                 project wiring → verification suite → report.

setup/scripts/
  memory-export.py        NEW — JSON dump of all memories. Calls into
                                 claude_memory.db.queries via docker exec.
  memory-import.py        NEW — restore from JSON dump (insert with original IDs).
  backup-volume.sh        NEW — tar snapshot of johnny-five-data volume.
  restore-volume.sh       NEW — restore tar snapshot.

setup/cron/
  README.md               NEW — cron / Task Scheduler / launchd examples + rotation.

README.md                 UPDATE — link the new docs in a "Documentation" section,
                                    add /integrate quickstart pointer.
```

## Key decisions

1. **Marker comments on every appended block.** Both CLAUDE.md snippets and the hooks JSON additions are wrapped in `<!-- BEGIN johnny-five (v1) --> ... <!-- END johnny-five -->`. The `/integrate` command uses these to detect prior installs and update without duplicating. The version tag (`v1`) lets future versions migrate cleanly.

2. **`/integrate` is a markdown prompt, not a script.** Cross-platform (Windows / Mac / Linux) by default. Claude does the work; the file is the recipe.

3. **Backup is CLI-only.** Per design discussion, no new MCP tools. Scripts run inside the container or against the volume directly. Cron-friendly.

4. **`AGENT_NOTES.md` is grep-bait.** Filename and headings use terms Claude will search for (`memory_store fails`, `recall returns empty`, `wrong project_dir`). Optimized for retrieval, not narrative flow.

5. **No skill or plugin manifest.** Onboarding is "clone repo → open Claude in repo → run `/integrate`". A plugin manifest is future work; the slash command in the cloned repo's `.claude/commands/` is sufficient for now.

6. **`docs/INTEGRATION.md` stays as-is.** It's already a strong architectural doc. New docs cross-link to it; they don't duplicate it.

## Out of scope (this branch)

- New MCP tools (memory_export / memory_import) — CLI scripts cover the use case.
- LLM rerank — separate research thread.
- Plugin manifest / Claude Code plugin packaging — onboarding via cloned repo is sufficient now.
- Cloud sync / S3 backup automation — documented as a "you can wire this up" pattern; not shipped.
- Per-project hook variants — global hooks are correct; per-project is left to user judgment.

## Verification

After all files written:

1. `/integrate` runbook reads top-to-bottom without external context. A Claude reading it cold can execute every step.
2. Every state-check command in the runbook is tested by running it (current repo state).
3. Marker-bracketed snippets in `CLAUDE_MD_SNIPPETS.md` are byte-identical to what the runbook tells Claude to append.
4. Backup scripts run end-to-end on a real container (export → import roundtrip).
5. README links resolve and don't duplicate sections from new docs.
6. `docs/INTEGRATION.md` references are still accurate after new docs land.

## Citations to verify before merge

Research returned canonical-from-training URLs for arXiv (MemGPT, Generative Agents, MemoryBank, Lost in the Middle) and Anthropic memory tool docs. The MCP citations are verified against `modelcontextprotocol.io`. Before merging the PR I'll re-fetch the unverified URLs from a less-restricted environment and update `BEST_PRACTICES.md` if any quotes need adjustment.
