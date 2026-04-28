# Backup & Restore

Operations runbook for backing up and restoring johnny-five memories. Three modes, ordered by recommended use:

1. **Volume snapshot** — fastest, captures everything (DB + WAL state).
2. **Export / import** — portable across schema versions, machine-readable.
3. **Scheduled** — automate either of the above with cron / Task Scheduler / launchd.

A **disaster recovery** section at the end covers "I lost the volume" recovery from a tarball.

---

## Mode 1: Volume snapshot (recommended)

The Docker named volume `johnny-five-data` contains everything: the SQLite DB, the WAL journal, sqlite-vec extension state. A `tar` of the volume is a complete, restorable backup.

### Create a snapshot

`setup/scripts/backup-volume.sh`:

```bash
./setup/scripts/backup-volume.sh                   # writes to ./j5-backups/
./setup/scripts/backup-volume.sh /custom/path      # writes to /custom/path
```

What it does (under the hood):

```bash
docker run --rm \
  -v johnny-five-data:/data:ro \
  -v "$(pwd)":/backup \
  alpine \
  tar czf "/backup/j5-volume-$(date +%Y%m%d-%H%M%S).tar.gz" -C /data .
```

The `:ro` mount means the script never writes to the live data — safe to run while Claude Code is connected.

### Restore from snapshot

`setup/scripts/restore-volume.sh`:

```bash
./setup/scripts/restore-volume.sh j5-backups/j5-volume-20260428-103045.tar.gz
```

What it does:

1. Stops the johnny-five container if running.
2. Wipes the existing volume contents.
3. Extracts the tarball into the volume.
4. Starts the container.

The script asks for confirmation before wiping — pass `-y` to skip the prompt for unattended use.

### Trade-offs

| | Volume snapshot |
|---|---|
| Speed | <1s for typical sizes (single tar, no DB walk) |
| Size | ~1KB per memory + WAL overhead. 1000 memories ≈ 1MB |
| Portability | Same OS family; tar.gz is universal but volume contents are SQLite-specific |
| Schema-version safe? | Yes — restored byte-for-byte |
| Live-safe? | Yes (read-only mount) |
| Best for | Routine backups, before image upgrades, pre-experiment safety nets |

---

## Mode 2: Export / import (JSON)

For migrating to a new machine, sharing memories between users, or surviving a major schema change. Trades some metadata fidelity (FTS index state) for human-readable, version-stable JSON.

### Export

`setup/scripts/memory-export.py`:

```bash
python setup/scripts/memory-export.py > j5-export-$(date +%Y%m%d).json
python setup/scripts/memory-export.py --project-dir /path/to/project > project-only.json
python setup/scripts/memory-export.py --tier hot --tier warm > active-only.json
```

What gets exported (per memory):

```json
{
  "id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
  "content": "User prefers Postgres over Mongo",
  "summary": null,
  "type": "user",
  "tags": ["preferences", "database", "forever-keep"],
  "importance": 9.0,
  "tier": "hot",
  "project_dir": "/path/to/project",
  "created_at": "2026-04-28T10:30:45Z",
  "updated_at": "2026-04-28T10:30:45Z",
  "last_accessed": "2026-04-28T11:00:00Z",
  "access_count": 3,
  "source_session": "session-abc123",
  "supersedes": null,
  "consolidated_from": [],
  "metadata": {}
}
```

Embeddings are **not** included — they're rebuilt on import (deterministic given the same model). Keeps export size small and avoids embedding-format coupling.

### Import

`setup/scripts/memory-import.py`:

```bash
python setup/scripts/memory-import.py < j5-export-20260428.json
python setup/scripts/memory-import.py --merge < shared-team-memories.json
python setup/scripts/memory-import.py --dry-run < j5-export-20260428.json
```

Modes:

- **default**: insert with original IDs. Fails fast if any ID already exists. Use for restoring to a fresh DB.
- **`--merge`**: insert if ID not present; update if present (preserves access counts and timestamps from the import). Use for combining two DBs.
- **`--dry-run`**: print what would be done, change nothing.

After import, embeddings are regenerated automatically. Allow ~30 seconds for first batch (model load); subsequent batches are <1s per 100 memories.

### Trade-offs

| | Export / import |
|---|---|
| Speed | Seconds for export; minutes for import (re-embedding) |
| Size | ~500 bytes per memory (JSON, no embeddings) |
| Portability | Universal — JSON is portable to any system |
| Schema-version safe? | Yes — import script tolerates added fields, ignores removed ones |
| Live-safe? | Yes — read-only DB query for export |
| Best for | New machine, sharing memories, major version upgrades |

---

## Mode 3: Scheduled backups

Automate Mode 1 (volume snapshot) on a recurring schedule. See `setup/cron/README.md` for platform-specific examples; the gist:

### Linux / macOS (cron)

```cron
# Weekly Sunday 3am: snapshot, retain last 8 weekly + 12 monthly
0 3 * * 0 /path/to/johnny-five/setup/scripts/backup-volume.sh ~/j5-backups/weekly && /path/to/johnny-five/setup/scripts/rotate-backups.sh ~/j5-backups
```

### Windows (Task Scheduler)

PowerShell wrapper that calls the bash script via Git Bash or WSL — see `setup/cron/README.md`.

### macOS (launchd)

A `.plist` file under `~/Library/LaunchAgents/` invoking the same script — also in `setup/cron/README.md`.

### What to back up where

| Backup target | Pros | Cons |
|---|---|---|
| Local disk | Fast, no auth, no quota | Lost if disk dies |
| External drive | Survives disk failure | Requires drive to be plugged in |
| Cloud-synced folder (Dropbox, iCloud Drive, OneDrive) | Off-site, automatic | Cloud provider sees your memories — encrypt first if sensitive |
| S3 / GCS / Azure | Off-site, durable, queryable | Auth setup, egress costs (rarely matter for small DBs) |

Most users: weekly local snapshot + monthly off-site copy is plenty. The DB is small; durability is cheap.

### Encryption

If the contents matter (you've been storing secrets — which you shouldn't but it happens), encrypt the tarball before it leaves the machine:

```bash
./setup/scripts/backup-volume.sh && \
  gpg --symmetric --cipher-algo AES256 j5-volume-*.tar.gz && \
  rm j5-volume-*.tar.gz   # remove unencrypted original
```

Restore: `gpg --decrypt < file.tar.gz.gpg > file.tar.gz` then run `restore-volume.sh`.

---

## Disaster recovery

### Scenario: container is gone, volume is gone, but you have a tarball

```bash
# 1. Rebuild the image (from a fresh clone of johnny-five repo)
git clone https://github.com/coreConvention/johnny-five.git
cd johnny-five
docker build -t johnny-five:latest .

# 2. Create the named volume
docker volume create johnny-five-data

# 3. Restore the tarball into it
./setup/scripts/restore-volume.sh /path/to/j5-volume-20260428.tar.gz

# 4. Verify
docker exec johnny-five python -c "
import asyncio
from claude_memory.mcp.tools import tool_memory_stats
print(asyncio.run(tool_memory_stats()))
"
# Expect: { "total": <expected count>, ... }
```

### Scenario: you have a JSON export but no tarball

```bash
# 1. Build image, create volume, run a fresh container
docker build -t johnny-five:latest .
docker run -d --name johnny-five -i -v johnny-five-data:/data johnny-five:latest

# 2. Wait for first start (model download if not pre-cached)
sleep 30

# 3. Import
python setup/scripts/memory-import.py < j5-export-20260428.json

# 4. Verify
python setup/scripts/memory-export.py | jq '. | length'
# Expect: <count from original export>
```

### Scenario: container is fine, but you accidentally `memory_forget`-ed something important

If the memory was archived (soft-deleted, `archive=true`):

```bash
# Promote it back from archived tier
docker exec johnny-five python -c "
import sqlite3
conn = sqlite3.connect('/data/memory.db')
conn.execute(\"UPDATE memories SET tier = 'hot' WHERE id = ?\", ('01ARZ3...YOUR-ID...',))
conn.commit()
"
```

If the memory was permanently deleted (`archive=false`):

You need a backup. This is the case the volume snapshot was made for.

---

## Verification after any restore

Always verify a restore worked before assuming the data is back:

```bash
# 1. Container is running
docker ps --filter name=johnny-five

# 2. DB is queryable
docker exec johnny-five python -c "
import asyncio
from claude_memory.mcp.tools import tool_memory_stats
print(asyncio.run(tool_memory_stats()))
"

# 3. Embeddings are intact (search works)
docker exec johnny-five python -c "
import asyncio
from claude_memory.mcp.tools import tool_memory_search
print(asyncio.run(tool_memory_search(query='test', top_k=3)))
"

# 4. Restart Claude Code so the MCP server reconnects, then run memory_stats from inside Claude
```

If any step fails, the restore is incomplete. Don't trust silent success.

---

## What's NOT backed up by these scripts

- `~/.claude/CLAUDE.md` (your global instructions) — backup separately
- `~/.claude/hooks/*.sh` — these come from the J5 repo, restore by re-cloning
- `~/.claude/settings.json` — backup separately; small file, easy to git
- Per-project `.claude/memory/lessons.md` if you use file-based backup of lessons — this lives in your project repo and should be tracked there

The volume snapshot covers **memories only**. Configuration is your responsibility.
