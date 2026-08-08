# Repair double-encoded consolidation rows in the live DB — execution prompt

> **Task:** close [`coreConvention/johnny-five#12`](https://github.com/coreConvention/johnny-five/issues/12)
> — a one-off, idempotent, **human-confirmed** data migration that repairs rows
> whose `tags` / `consolidated_from` / `metadata` were written double-JSON-encoded
> before the write-path fix landed. One session → one PR that `Closes #12`.
> **This is NOT Tier A work; the Tier A scoped commit grant does NOT apply here —
> the global commit gate holds (see §6).**

**Binding refs (read first, in order):**
- Issue [#12](https://github.com/coreConvention/johnny-five/issues/12) — the repair
  spec: detection rule, the idempotent snippet, and acceptance criteria. **It is the
  source of truth for WHAT to do;** this file is the execution contract for HOW.
- Issue [#11](https://github.com/coreConvention/johnny-five/issues/11) (closed) — the
  root cause + a stdlib-only reproduction.
- PR [#13](https://github.com/coreConvention/johnny-five/pull/13) (merged) — the
  **write-path fix**. `run_consolidation` no longer double-encodes, so **no new bad
  rows are produced**; this task only repairs rows written *before* #13.
- Design doc `.claude/plans/2026-08-07-tier-a-memory-observability.md` §A3 (context).

---

## 1. State resolution (reality wins)

1. `memory_search` johnny-five for `"double encode consolidated_from repair"` and
   `"updated_at created_at aging"` — load prior context (esp. j5 memory
   `01KZGYMJMYYAMWFQCCEC69HW59` "surfacing exposes latent bugs" and the Tier A
   project memory `01KZGYMXE7V4G6REFK0Q1BB3Q4`).
2. Confirm #12 is still **open** and unclaimed:
   `env -u GITHUB_TOKEN -u GH_TOKEN gh issue view 12 --repo coreConvention/johnny-five --json state`,
   and `gh pr list --repo coreConvention/johnny-five --state open` (no existing repair PR).
3. Confirm the write path on `main` is already fixed (guards against re-doing #11):
   `src/claude_memory/lifecycle/consolidation.py` builds the consolidated record with
   **native** `tags` / `consolidated_from=cluster_ids` / `metadata={}` (no `json.dumps`
   at the record-construction site). If it still pre-`json.dumps`es, STOP — #13 is not
   actually on your base; branch off the merge that contains it.
4. Branch off latest `main`: `chore/12-repair-double-encoded-rows` (or similar).

---

## 2. The bug, in one paragraph

The DB layer owns JSON encoding: `insert_memory` `json.dumps()`es `tags` /
`consolidated_from` / `metadata` once; `_row_to_record` `json.loads()`es once. Old
`run_consolidation` pre-`json.dumps`ed them, so those columns are encoded **twice** and
decoded **once** — the column holds e.g. `'"[\"a\",\"b\"]"'` and
`get_memory(...).consolidated_from` returns the **`str`** `'["a", "b"]'` instead of the
list. Read-side `isinstance`-or-`json.loads` defenses (`mcp/tools.py` serializers,
`retrieval/search.py::_parse_tags`, `consolidation.py::_is_pinned`, `_memory_why_dict`)
mask it today; the risk is any future reader that trusts the declared type. **Keep those
defenses** (belt-and-suspenders) — this migration removes the underlying corruption, it
does not replace the guards.

---

## 3. Deliverable

1. **An idempotent repair function** — e.g. `repair_double_encoded_json(conn, *, dry_run=False) -> dict`
   in `src/claude_memory/db/queries.py` (or a small `db/migrations.py`). For each of
   `tags`, `consolidated_from`, `metadata` on every row: `decoded = json.loads(raw)`; if
   `isinstance(decoded, str)` (a `list`/`dict` was expected) the row is double-encoded and
   that inner string is the correct single-encoded value → `UPDATE ... SET <col> = decoded`.
   Return per-column counts of `{scanned, repaired}`. `dry_run=True` counts without writing.
   - **Idempotent by construction:** a second pass decodes to a `list`/`dict`, so the
     `isinstance(decoded, str)` test is false and nothing changes. Re-runnable safely.
   - **Only the three columns.** Do not touch any other column. Do not hard-delete anything.
   - **Note the `tags` FTS coupling:** `memories` has an `AFTER UPDATE` trigger
     (`memories_au`) that re-syncs `memories_fts`. A plain `UPDATE memories SET tags=...`
     fires it and keeps FTS consistent — **do not** disable triggers or write FTS directly.
2. **A unit test** (`tests/test_migrations.py` or alongside `tests/test_queries.py`,
   mirroring `tests/conftest.py`'s in-memory fixture) proving: (a) a double-encoded row is
   repaired to native types, (b) a healthy (single-encoded) row is left byte-for-byte
   untouched, (c) re-running is a no-op (`repaired == 0` on the second pass).
3. **A safe way to run it against the live DB** — a tiny CLI/entry or a documented
   invocation (see §4). Prefer reusing the function above so the same code the test covers
   is what runs in production.

---

## 4. SAFETY — this migration writes the live production memory DB

The live corpus is the johnny-five MCP container's SQLite DB: container
`johnny-five-johnny-five-1` (compose at `z:/personal/johnny-five`), volume
`johnny-five-data` mounted at `/data`, `MEMORY_DB_PATH=/data/memory.db`, WAL mode with a
5 s `busy_timeout`. **Do not skip any step:**

1. **BACK UP FIRST.** Copy the DB out before touching it, e.g.
   `docker cp johnny-five-johnny-five-1:/data/memory.db ./memory.db.pre-repair.bak`
   (and the `-wal`/`-shm` siblings if present). Keep the backup until verified.
2. **COUNT + DRY-RUN before any write.** Run the repair with `dry_run=True` and report the
   per-column affected counts. **If zero rows are affected, there is nothing to repair —
   ship the function + test (they guard the future) and say so; do not write to the DB.**
3. **HUMAN CONFIRM before the live write.** Present the dry-run counts and get an explicit
   go-ahead before running the real migration against `/data/memory.db`.
4. **Minimize concurrent-write risk.** WAL + `busy_timeout` tolerate the running MCP
   server, but for a clean apply prefer briefly stopping it
   (`docker compose -f z:/personal/johnny-five/docker-compose.yml stop johnny-five`),
   running the migration, then starting it — or run inside the container via `docker exec`.
   Either way, the backup from step 1 is the real safety net.
5. **Idempotent + verified.** Re-run after applying; the second pass must report 0 repaired.

If the container / live DB is unreachable this session (Docker down, etc.), that is a
**hard blocker** for the live-apply half — still ship the function + test + dry-run
tooling in the PR, note the live-apply is pending DB availability, and hand back a resume
note. Do not fabricate a live-apply you did not run.

---

## 5. Verify & Proof (paste actual output)

```bash
PYTHONPATH=src python -m pytest tests/ -q          # all green incl. the new repair test
```
- Dry-run count on the live DB (or a `docker cp`-ed copy) — the number of double-encoded
  rows per column.
- After apply (if run): `get_memory(<a-repaired-id>)` returns `list`/`dict` (not `str`) for
  the affected fields; `memory_why(<consolidated-row-id>)` returns `consolidated_from` as a
  real list; a second dry-run reports **0** rows to repair.
- Existing suite still green (read-side defenses untouched).

---

## 6. Commit / PR gate  ·  issue-close discipline

- **The Tier A §4 commit grant does NOT extend here.** The global CLAUDE.md hard gate
  applies: **do not `git commit` without a live "commit"** from Brandon in the moment.
  Finish the code + test, run the dry-run, stage, **STOP**, and report "ready to commit."
- Issue-first is already satisfied (**#12 exists**). Open the PR with **`Closes #12`**,
  labels **`bug`, `hygiene`**. Reference #11 (root cause) and #13 (write-path fix).
- Follow the CLAUDE.md security discipline for `gh`: write the PR/issue body to a file via a
  quoted heredoc and pass `--body-file`; never inline backticks/`$`; clear
  `GITHUB_TOKEN`/`GH_TOKEN` before public `gh` writes.
- **STOP at the merge line** — Brandon reviews and merges.

## 7. Anti-scope

- **Do not** re-fix the write path (already fixed in #13) — this is data repair only.
- **Do not** remove the read-side normalization defenses (`isinstance`-or-`json.loads`) —
  they stay as belt-and-suspenders.
- **Do not** touch any column other than `tags` / `consolidated_from` / `metadata`, and
  **never hard-delete** a row. Archival + `supersedes` remain the only removal mechanisms.
- Anything architectural not settled by #12/#11 (a schema change, a new table) → STOP and ask.

---
*Basis: issues #11/#12 + Tier A design doc §A3. Companion to the shipped write-path fix #13.*
