# AGENTS.md snippets for Johnny-Five

Codex reads global instructions from `~/.codex/AGENTS.md` and project instructions from `AGENTS.md`. Use the installer-owned marker block below so upgrades can replace Johnny-Five guidance without touching unrelated instructions.

## Global Codex block

The canonical block lives in [`../setup/codex/AGENTS.md.snippet`](../setup/codex/AGENTS.md.snippet). Install or update it with:

```powershell
python setup\scripts\install-codex.py --dry-run
python setup\scripts\install-codex.py --install
```

The block establishes these invariants:

- Johnny-Five is the primary persistent memory store.
- Global hooks already perform session recall; do not repeat it manually at startup.
- Project scope comes from hook payload `cwd` or the current working directory.
- Search happens before investigation, authoring decisions, corrections, and stores.
- The only runtime is the canonical `johnny-five` SSE container on port 8787.
- A restored service requires a fresh Codex task before MCP tools can appear.

## Project hint

Projects need only a short hint when their main `AGENTS.md` does not already describe Johnny-Five:

```markdown
## Memory

Johnny-Five is configured globally. Scope manual `memory_*` calls to this repository's current working directory. Search before investigating or deciding, and update matching memories instead of storing duplicates.
```

Do not install the same Johnny-Five hooks again at project level. Codex runs matching hooks from every active layer concurrently, so duplicate global and project registrations cause duplicate searches and counter updates.
