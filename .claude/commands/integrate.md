---
description: Integrate johnny-five into Claude Code or Codex with platform-specific manifests and verification.
---

# /integrate

Set up Johnny-Five end-to-end using the one canonical SSE service and the selected client's native configuration.

**Read this entire file before acting.** Don't skip steps. Don't paraphrase the marker-bracketed snippets — copy them verbatim.

This command is **idempotent** — re-running on a partial install is safe. State detection in Step 1 tells you what's already done.

---

## Step 0: Read the full runbook

Before any action, scan:

1. This file, top to bottom.
2. [`docs/CLAUDE_MD_SNIPPETS.md`](../../docs/CLAUDE_MD_SNIPPETS.md) — the marker-bracketed blocks you'll be appending.
3. [`docs/AGENT_NOTES.md`](../../docs/AGENT_NOTES.md) — failure modes you'll reference if anything breaks.

If a step says "see snippet block 1" — open `CLAUDE_MD_SNIPPETS.md` and use the byte-for-byte content between the BEGIN/END markers.

### Choose the client before continuing

Ask whether the target is Claude Code or Codex.

- **Codex:** run `python setup/scripts/install-codex.py --dry-run`, show the proposed merge, then run `--install` after confirmation and `--verify`. Report [`setup/codex/config.toml.snippet`](../../setup/codex/config.toml.snippet) for project MCP configuration and have the user review `/hooks` trust. Stop after the Codex verification path; do not execute the Claude-specific steps below.
- **Claude Code:** continue with this runbook and the Claude files named below.

Never register Johnny-Five at both global and project hook layers in Codex. Matching hooks from active layers run concurrently.

---

## Step 1: Detect state (run all checks in parallel)

Run these checks. The outputs branch the rest of the runbook.

### 1a. Docker is installed and running

```bash
docker info >/dev/null 2>&1 && echo "DOCKER_OK" || echo "DOCKER_MISSING"
```

- `DOCKER_OK` → continue
- `DOCKER_MISSING` → STOP. Tell the user "Docker is required. Install Docker Desktop or Docker Engine, start it, and re-run /integrate." Don't try to install Docker yourself — that's user-level system config.

### 1b. johnny-five image exists

```bash
docker image inspect johnny-five:latest >/dev/null 2>&1 && echo "IMAGE_OK" || echo "IMAGE_MISSING"
```

### 1c. johnny-five container state

```bash
docker ps -a --filter name=johnny-five --format "{{.Status}}" | head -1
```

- Output starting with `Up` → running
- Output starting with `Exited` → stopped
- Empty → no container

### 1d. Global MCP config

```bash
cat ~/.claude/mcp.json 2>/dev/null | grep -q "johnny-five" && echo "MCP_OK" || echo "MCP_MISSING"
```

(Note: this checks `~/.claude/mcp.json`. Some setups use `~/.claude/settings.json` for MCP — check both.)

### 1e. Global hooks present

```bash
for h in session-start-recall.sh memory-context-inject.sh user-prompt-correction.sh precompact-enforce.sh tool-failure-tracker.js memory-discipline-track.js memory-discipline-enforce.js; do
  test -f ~/.claude/hooks/$h && echo "HOOK_${h}_OK" || echo "HOOK_${h}_MISSING"
done
```

### 1f. Global CLAUDE.md has the snippet

```bash
grep -q "BEGIN johnny-five" ~/.claude/CLAUDE.md 2>/dev/null && echo "CLAUDEMD_OK" || echo "CLAUDEMD_MISSING"
```

### 1g. Hooks registered in settings.json

```bash
test -f ~/.claude/settings.json && grep -q "session-start-recall.sh\|precompact-enforce.sh" ~/.claude/settings.json && echo "SETTINGS_OK" || echo "SETTINGS_MISSING"
```

### 1h. Where am I (the J5 repo we're integrating from)

```bash
pwd && git remote get-url origin 2>/dev/null
```

Confirm you're inside a johnny-five clone. If not, STOP and tell the user "Run /integrate from inside a clone of the johnny-five repo."

---

## Step 2: Ask the user what scope to integrate

Use **AskUserQuestion** with these two questions:

**Q1: Integration scope?**
- Global only (set up `~/.claude/` — works across all projects)
- Project only (assume global is already set up, wire one project)
- Both — set up global AND wire one specific project (Recommended for first-time install)

**Q2 (only if scope includes project): Target project path?**
- Provide an absolute path. Verify it exists with `test -d <path>`.

If user picks "Both" without a project path, ask Q2.

---

## Step 3: Confirm the plan and risks

Before any writes, summarise to the user:

- What will be created (image, container, hooks, snippets)
- What will be edited (settings.json, CLAUDE.md — both will get marker-bracketed appends)
- What is NOT touched (existing memories in `johnny-five-data` volume, existing settings outside the marker blocks)
- Estimated time (10–30 min depending on whether the embedding model has to download)

Use AskUserQuestion to get final confirmation: "Proceed with these changes?" Yes / No.

---

## Step 4: Global wiring (skip subitems already detected as OK)

Execute in this order. Verify after each.

### 4a. Build the image (if `IMAGE_MISSING`)

```bash
docker build -t johnny-five:latest .
```

Verify: `docker image inspect johnny-five:latest >/dev/null && echo OK`

### 4b. Create / start the container

If the canonical service is missing, first confirm `docker ps -a` contains no other Johnny-Five-like container and then use Compose:
```bash
docker volume create johnny-five-data
docker compose up -d
```

If container exists but stopped:
```bash
docker start johnny-five
```

If container is already running, no-op.

Verify: `docker ps --filter name=johnny-five --format "{{.Status}}"` returns a string starting with `Up`.

### 4c. Register MCP server in `~/.claude/mcp.json` (if `MCP_MISSING`)

If `~/.claude/mcp.json` doesn't exist, create it with the SSE endpoint:

```json
{
  "mcpServers": {
    "johnny-five": {
      "type": "sse",
      "url": "http://127.0.0.1:8787/sse/"
    }
  }
}
```

If it exists with other servers, parse it as JSON, add only the `johnny-five` key, write a timestamped backup, and preserve every unrelated server. Verify the resulting file parses and the URL ends in `/sse/`.

### 4d. Copy hooks to `~/.claude/hooks/` (skip ones already OK)

```bash
mkdir -p ~/.claude/hooks
cp setup/hooks/session-start-recall.sh ~/.claude/hooks/
cp setup/hooks/memory-context-inject.sh ~/.claude/hooks/
cp setup/hooks/precompact-enforce.sh ~/.claude/hooks/
cp setup/hooks/user-prompt-correction.sh ~/.claude/hooks/
cp setup/hooks/tool-failure-tracker.js ~/.claude/hooks/
cp setup/hooks/memory-discipline-track.js ~/.claude/hooks/
cp setup/hooks/memory-discipline-enforce.js ~/.claude/hooks/
mkdir -p ~/.claude/hooks/lib
cp setup/hooks/lib/j5-runtime.sh setup/hooks/lib/j5-runtime.js ~/.claude/hooks/lib/
chmod +x ~/.claude/hooks/*.sh
```

Verify each:
```bash
test -x ~/.claude/hooks/session-start-recall.sh && echo OK
test -x ~/.claude/hooks/memory-context-inject.sh && echo OK
test -x ~/.claude/hooks/precompact-enforce.sh && echo OK
test -x ~/.claude/hooks/user-prompt-correction.sh && echo OK
test -f ~/.claude/hooks/tool-failure-tracker.js && echo OK
test -f ~/.claude/hooks/memory-discipline-track.js && echo OK
test -f ~/.claude/hooks/memory-discipline-enforce.js && echo OK
test -f ~/.claude/hooks/lib/j5-runtime.js && echo OK
```

### 4e. Register hooks in `~/.claude/settings.json` (if `SETTINGS_MISSING`)

The canonical hooks JSON is in `setup/hooks.json.enforced.snippet`.

If `~/.claude/settings.json` doesn't exist:
```bash
cp setup/hooks.json.enforced.snippet ~/.claude/settings.json
```

If it exists, parse both JSON files and merge individual Johnny-Five handler entries without replacing unrelated event arrays. Back up the original first.

**Caution**: if the user has existing `PostToolUse` or `UserPromptSubmit` hooks, the deep-merge may overwrite them. Before running the merge, check for collisions:
```bash
jq '.hooks | keys' ~/.claude/settings.json
```

If hooks like `PostToolUse` or `UserPromptSubmit` already exist, **stop and ask the user** before merging — they may need a manual review.

Verify: `jq '.hooks.SessionStart' ~/.claude/settings.json` shows the `session-start-recall.sh` entry.

### 4f. Append snippet to `~/.claude/CLAUDE.md` (if `CLAUDEMD_MISSING`)

Open [`docs/CLAUDE_MD_SNIPPETS.md`](../../docs/CLAUDE_MD_SNIPPETS.md). Find "Block 1: Global snippet". Copy everything between the lines:

```
<!-- BEGIN johnny-five (v1) -->
```

and

```
<!-- END johnny-five -->
```

(inclusive of both marker lines).

Append to `~/.claude/CLAUDE.md`. If the file doesn't exist, create it with this content. Add a blank line before the BEGIN marker if the file isn't empty.

Verify:
```bash
grep -E "BEGIN johnny-five|END johnny-five" ~/.claude/CLAUDE.md
```
Should print both markers.

---

## Step 5: Project wiring (skip if scope is "global only")

For the target project path from Q2:

### 5a. Add to project `.mcp.json` (if not already present)

```bash
PROJECT_PATH="<from Q2>"
test -f "$PROJECT_PATH/.mcp.json" && cat "$PROJECT_PATH/.mcp.json" | grep -q "johnny-five" && echo "already wired"
```

If not present, create or merge into `$PROJECT_PATH/.mcp.json`:

```json
{
  "mcpServers": {
    "johnny-five": {
      "type": "sse",
      "url": "http://127.0.0.1:8787/sse/"
    }
  }
}
```

(Same content as the global `mcp.json`. Per-project takes precedence at runtime, so wiring per-project is useful when sharing a project across machines.)

### 5b. Append project snippet to `<project>/CLAUDE.md` (if user wants it)

Use AskUserQuestion: "Append a per-project johnny-five hint to `<project>/CLAUDE.md`?"
- Yes — append the marker-bracketed Block 2 from `CLAUDE_MD_SNIPPETS.md`
- No — skip (global discipline rules apply anyway)

If yes, open `CLAUDE_MD_SNIPPETS.md` Block 2, copy verbatim between markers, append to `<project>/CLAUDE.md`.

### 5c. Optional: create `.claude/memory/lessons.md` skeleton

Use AskUserQuestion: "Create a file-based backup for project lessons (`.claude/memory/lessons.md`)?"
- Yes — create the file with a header explaining its purpose:

```markdown
# Lessons (file-based backup)

Each lesson stored via `memory_store` should also append a structured entry here. The file is grep-able even when the johnny-five container is down. Format:

## [Category] Description
- **Date**: YYYY-MM-DD
- **Mistake**: ...
- **Correction**: ...
- **Rule**: ...

---

```

- No — skip.

---

## Step 6: Verification suite

All three must pass. If any fails, stop and report.

### 6a. Container reachable

```bash
docker exec johnny-five python -c "
import asyncio
from claude_memory.mcp.tools import tool_memory_stats
print(asyncio.run(tool_memory_stats()))
"
```
Expected: a dict with `by_type`, `by_tier`, `total`. For a fresh install, `total: 0` is fine.

### 6b. Hooks fire correctly

```bash
printf '{"session_id":"verify","cwd":"%s","hook_event_name":"SessionStart","source":"startup"}' "$(pwd)" | bash ~/.claude/hooks/session-start-recall.sh | head -c 500
```
Expected: JSON with `hookSpecificOutput.additionalContext`. The `additionalContext` may be empty if the DB is fresh — that's OK; what matters is the JSON structure is valid.

```bash
printf '{"session_id":"verify","cwd":"%s","hook_event_name":"PreCompact","trigger":"manual"}' "$(pwd)" | bash ~/.claude/hooks/precompact-enforce.sh
```
Expected: JSON with `"continue": true`.

### 6c. End-to-end memory roundtrip

```bash
docker exec johnny-five python -c "
import asyncio
from claude_memory.mcp.tools import tool_memory_store, tool_memory_search

async def main():
    r1 = await tool_memory_store(content='integration smoke test', type='project', importance=5.0, tags=['integrate-smoketest'])
    print('STORED:', r1)
    r2 = await tool_memory_search(query='integration smoke test', top_k=3)
    print('SEARCHED:', r2)

asyncio.run(main())
"
```
Expected: STORED returns `action: inserted` with a memory_id. SEARCHED returns the same memory_id in results.

After verification: clean up the smoke-test memory:

```bash
docker exec johnny-five python -c "
import asyncio, json
from claude_memory.mcp.tools import tool_memory_search, tool_memory_forget

async def main():
    r = await tool_memory_search(query='integration smoke test', top_k=3)
    for hit in r.get('results', []):
        if 'integrate-smoketest' in hit.get('tags', []):
            await tool_memory_forget(memory_id=hit['id'], archive=False)
            print('CLEANED:', hit['id'])

asyncio.run(main())
"
```

---

## Step 7: Final report

Print a summary using this template:

```
✓ johnny-five integration complete

What was set up:
- Image: johnny-five:latest <built|already present>
- Container: johnny-five <created|started|already running>
- Global MCP config: ~/.claude/mcp.json <created|updated|already present>
- Global hooks: seven handlers plus shared runtime helpers in ~/.claude/hooks/
- Global hooks registered in: ~/.claude/settings.json
- Global CLAUDE.md: appended johnny-five (v1) snippet
[if project wiring]
- Project MCP config: <path>/.mcp.json
- Project CLAUDE.md hint: <path>/CLAUDE.md (if user opted in)
- Project lessons.md: <path>/.claude/memory/lessons.md (if user opted in)

Verification:
✓ Container reachable (memory_stats)
✓ session-start-recall hook fires
✓ precompact-enforce hook fires
✓ Memory store/search roundtrip succeeded

Next steps:
1. RESTART Claude Code (this session won't see the new MCP server until restart).
2. Read docs/BEST_PRACTICES.md for the discipline rules.
3. Set up scheduled backups: see docs/BACKUP_AND_RESTORE.md.
4. To verify post-restart: ask Claude to "run memory_stats" — you should see total: 0 (or your existing count if you restored from a backup).

Any failures along the way? See docs/AGENT_NOTES.md for diagnostics.
```

---

## Failure modes (consult this section if a step fails)

### Image build fails
- "no such file or directory" → not in J5 repo. `pwd` and verify a `Dockerfile` exists.
- Disk space → `docker system df` to check; `docker system prune` to free space.
- Network timeout pulling base image → user is offline / rate-limited; retry later.

### Container won't start
- "container name already in use" → stop and inspect existing container ownership, mounts, ports, and logs. Never create a duplicate.
- Container starts then immediately exits → check `docker logs johnny-five` and the canonical Compose configuration.
- "no such image" → image build was skipped or failed; rebuild.

### `memory_stats` fails inside container
- "module not found" → image is corrupt; rebuild from clean.
- "no such table" → fresh volume but DB never initialized; restart the container so `initialize_db` runs.

### Hooks don't fire after Claude Code restart
- Settings.json malformed → `jq . ~/.claude/settings.json` should print the file. If it errors, your merge introduced bad JSON; fix the syntax.
- Hook scripts not executable → `chmod +x ~/.claude/hooks/*.sh`
- Path mismatch in settings.json → entries should be `~/.claude/hooks/<script>` not absolute paths starting with `/Users/...`

### Snippet appended twice
- Detected by `grep -c "BEGIN johnny-five" ~/.claude/CLAUDE.md` returning >1.
- Fix: open the file, delete the duplicate block (keep the most recent one). The marker comments make it easy to identify the boundaries.

### Settings.json merge clobbered existing hooks
- Restore from backup if you made one.
- If not: `jq '.hooks | keys' ~/.claude/settings.json` to see what survived. Manually re-add the lost hooks from your version control or memory.

---

## What you must NOT do

- ❌ `docker rm -f johnny-five-data` or any volume manipulation — the volume contains the user's memories.
- ❌ Edit the SQLite DB file directly — go through MCP tools or `setup/scripts/`.
- ❌ Skip Step 6 (verification) — silent success is the worst failure mode.
- ❌ Re-run Step 4f without checking for existing markers — that double-appends the snippet.
- ❌ Modify `setup/hooks/*.sh` to "fit better" — those are versioned scripts; if they need changes, that's a J5 PR not an integration step.
- ❌ Tell the user "everything is fine" if any verification step failed. Report failures explicitly.

---

## Re-running on an existing install

This command is idempotent. On re-run:

- Image build: skipped (image exists).
- Container: started (if stopped) or no-op (if running).
- MCP config: `jq` merge is idempotent (same key, same value).
- Hooks: `cp` overwrites with current versions (this is correct — keeps users on the latest hook scripts).
- Settings.json: `jq` deep-merge is idempotent on the registered hook entries.
- CLAUDE.md snippet: detected by markers; if `v1` markers exist, no append. If a future `v2` ships, prompt to upgrade.
- Project wiring: re-confirm with the user before re-applying.

If the user reports issues after a fresh /integrate, run Step 1 again to see what's actually present, and Step 6 to see what's actually working. Don't assume the install state from prior conversation.
