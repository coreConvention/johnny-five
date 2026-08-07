# Tier A — Memory Observability & Hygiene · Execution Orchestrator

> **Operating mode:** multi-PR build · one repo (`coreConvention/johnny-five`) · strictly sequential ·
> **one work unit per session**, each ending in a merged PR that closes its tracking issue.
> Re-runnable: open this file at the start of every session, resolve state (§1), execute the next
> unstarted work unit, stop.

**Binding design doc:** [`.claude/plans/2026-08-07-tier-a-memory-observability.md`](../plans/2026-08-07-tier-a-memory-observability.md)
— read it in full before touching code. It is the source of truth; this file is the execution
contract that carries it out. Any conflict → the design doc wins; if the design doc is silent on an
architectural question, **STOP and ask** (§7).

**Work units, in order (do not reorder):**

| WU | Design ref | Deliverable | Risk | Gate to start |
|----|-----------|-------------|------|---------------|
| WU-1 | A2 | Surface provenance + retrieval-stats on search/stats + `memory_why` | LOW | none |
| WU-2 | A1 | Read-only dashboard: stand up the REST app + list endpoint + static page | MED | WU-1 merged |
| WU-3 | A3 | Contradiction-reconciliation in `consolidate` (human-confirmed) | MED (destructive-adjacent) | WU-2 merged |

WU-2 consumes the fields WU-1 exposes; WU-3 reuses WU-2's confirm surface. A later WU **may not
begin** until the prior WU's PR is merged to `main`.

---

## 1. State Resolution & Continuity — REALITY WINS OVER MEMORY

Compaction and memory drift both lie. At session start, resolve *where the program actually is*
before doing anything:

1. `memory_search` for `"tier A memory observability"` and the WU you think is next — load prior
   session-state and lessons.
2. **Cross-check against reality, which decides:**
   - `git -C Z:/Personal/johnny-five status` and `git log --oneline -8` (or the active worktree).
   - `env -u GITHUB_TOKEN -u GH_TOKEN gh pr list --repo coreConvention/johnny-five --state all --limit 10`
   - `env -u GITHUB_TOKEN -u GH_TOKEN gh issue list --repo coreConvention/johnny-five --state all --limit 10`
3. The **next WU** is the lowest-numbered one whose tracking issue is not yet closed by a merged PR.
   If memory and `gh`/`git` disagree, **reality wins** — trust the merged-PR/closed-issue state, not
   a session-state memory.
4. Before writing code, re-run `memory_search` scoped to the WU's surface (e.g. `"johnny-five
   SearchResultItem serializer parity"`) — search-first is per-decision, not per-session.

---

## 2. ANTI-RE-EXPANSION — the cuts are load-bearing

Tier A is a **converged plan**: it reached this scope by *cutting* components under review. A spec
that looks like it is "missing" a feature had that removed **deliberately**. Restoring any of the
following silently undoes the convergence — **escalate (§7), never improvise**:

- **Tier B is out of scope.** Do **not** add auto-extraction of memories, profile-derivation, or
  unscoped cross-project search. They increase supply, which is the exact problem w31rd #1553 found
  (corpus over-supplied; 29% never retrieved). More surface is the anti-goal.
- **Do not mount the dashboard into the MCP server.** A1 hosting option (b) — mounting the REST app
  into `server.py`'s Starlette app alongside `/sse` — was **rejected**. Stand up a **separate**
  service (option a) so the MCP stdio/sse path stays untouched. (See WU-2.)
- **Do not change what SessionStart injects.** Tier A is inspection/hygiene only. Never surface
  context by mutating tracked repo files — that is the IJFW anti-lesson the design doc's constraint
  encodes. The injection mechanism is not in scope for any WU.
- **Never hard-delete a memory.** Archival (`tier='archived'`) and `supersedes` pointers are the only
  removal mechanisms. This holds for the dashboard's "forget" (WU-2) and reconciliation (WU-3).

---

## 3. Model routing (standing policy)

| Role | Model | Use for |
|------|-------|---------|
| Orchestrate | Opus | This file; planning, sequencing, integration decisions |
| Implement | Sonnet | Writing the WU's code + tests |
| Review | Fable | The adversarial verification pass (§6) — dispatched with the diff |

Mechanical read-only fan-out (e.g. "grep for all call sites of `get_stats`") may pin a cheaper model.

---

## 4. Commit / PR gate  ·  scoped authorization

**Within this orchestration the executor is authorized to `git commit` and open PRs autonomously.**
This is a deliberate in-session grant from Brandon (2026-08-07) that scopes down the global commit
gate **for Tier A work only**. The human gate is **Brandon's manual merge** — PRs land in review, he
merges. Authorized work gets executed, not re-asked (j5 `01KR6VAZ0QKPBK1K45VT7BEW0G`).

**Boundaries:**
- **Do the commit + PR; do NOT merge.** No `gh pr merge`, no auto-merge, no branch cleanup of `main`.
  The merge is Brandon's, and it is what unblocks the next WU (§1 reads it from `gh` state).
- The grant does **not** generalize beyond Tier A. Outside this orchestration the global gate holds:
  never commit without a live "commit".

Each WU session therefore ends like this:
1. Finish the code + tests for the WU.
2. Run the WU's **Verify** and **Proof** (§ per-WU). Paste the actual command output.
3. Commit on the WU branch, push, and open the PR with `Closes #<issue>`, confirming it references
   the design doc.
4. **STOP at the merge line.** Report the PR link and the green verification. Brandon reviews and
   merges manually; the next WU does not begin until that merge lands.

**Branching:** one branch per WU off latest `main`
(`feat/tier-a-wu1-provenance-surfacing`, `feat/tier-a-wu2-dashboard`,
`feat/tier-a-wu3-reconciliation`). If working in a worktree, verify the branch is isolated before
dispatching any `isolation: "worktree"` subagent (a stale-base subagent can silently regress prior
work — j5 `01KR7EW4F1GQA4ZF8V6VVYXFGD`).

---

## 5. Error handling — friction vs. blocker

- **Class-A friction → fix in-session.** The j5 Docker container down → `docker start
  johnny-five-johnny-five-1`; missing dev deps → `pip install -e ".[dev]"` (or `uv sync`); port 8787
  conflict → find and free it; a failing unrelated test on `main` → note it, don't let it block.
  Contract-mandated verification that won't start is *your* job to make run, not skip.
- **Hard blocker → RESUME PROMPT + stop.** j5/MCP unavailable, missing credentials, a service that
  needs Brandon to provision, or a `main` regression outside this WU's scope. Emit a copyable resume
  prompt (what was attempted, exact blocker, branch/worktree state, files in flight, next concrete
  steps) — don't burn tokens flailing.

---

## 6. Per-WU adversarial mandate (dispatch VERBATIM with the diff)

After the WU's code is written and tests pass, dispatch a **Fable** reviewer with the WU's diff and
this instruction, substituting the WU-specific hunt list:

> You are an adversarial reviewer. Assume the diff is wrong until proven otherwise. Your job is to
> **refute** the claim that this WU is correct and safe. Default to "found a problem" if uncertain.
> Return concrete failure scenarios (inputs → wrong output/crash), not style notes. Hunt specifically
> for: **{WU HUNT LIST}**. Also confirm the anti-re-expansion invariants (§2) are not violated: no
> Tier-B surface added, no mount into `server.py`, no hard-delete, injection mechanism untouched.

Treat the reviewer's findings with technical rigor (verify, don't blindly implement or blindly
dismiss). Fix real findings before committing.

---

## 7. Architectural escalation

Anything architectural **not** already decided in the design doc → **STOP and ask Brandon** (via the
session, or an `AskUserQuestion` if interactive). Examples that require escalation: a new DB table or
column, a schema redesign, a new service layer beyond the single dashboard app, cross-module
refactoring, or any change to the MCP tool contract's existing shape. Auto-fix only what the current
WU caused (§ Deviation Rules in CLAUDE.md).

---

## 8. Key Files (every path resolves)

| Path | What's there |
|------|--------------|
| [`.claude/plans/2026-08-07-tier-a-memory-observability.md`](../plans/2026-08-07-tier-a-memory-observability.md) | **Binding design doc** |
| `src/claude_memory/api/routes.py` | `APIRouter` (L23), `SearchResultItem` (L59), `StatsResponse` (L103); **no `FastAPI()` app exists yet** |
| `src/claude_memory/mcp/tools.py` | `_search_result_to_dict` (L71), `_search_result_to_summary_dict` (L93), `tool_memory_stats` (L410) |
| `src/claude_memory/db/queries.py` | `MemoryRecord` (L12), `update_access` (L296), `get_memories_by_tier` (L319), `get_stats` (L334); **add** `list_memories(...)` |
| `src/claude_memory/db/schema.py` | `memories` DDL (L24-41) — `access_count`, `last_accessed`, `source_session`, `supersedes`, `consolidated_from`, `project_dir`, `tier` all exist |
| `src/claude_memory/server.py` | `list_tools` (L43), `run_sse` (L409), transport CLI (L472); mounts MCP `/sse` + `/messages/` only |
| `src/claude_memory/lifecycle/consolidation.py` | `find_clusters` (L58, cosine 0.75), `run_consolidation` (L193), `ConsolidationReport` (L23) |
| `tests/conftest.py` | fixtures `db_conn` (in-memory sqlite), `mock_encoder`, `sample_memories` |
| `tests/test_queries.py`, `tests/test_summary_only.py` | representative test patterns to mirror |
| `docker-compose.yml` | service `johnny-five`, `8787:8787`, `command: ["--transport","sse","--port","8787"]` |
| `pyproject.toml` | console script `johnny-five`, pytest config (`asyncio_mode="auto"`) |

---

## WU-1 · A2 — Provenance + retrieval-stats surfacing  ·  LOW risk

**Tracking issue:** open first — title *"A2: surface provenance + retrieval-stats on search/stats +
memory_why"*, labels: `enhancement`, `observability` (create labels if absent). Body references the
design doc §A2.

**Why first:** it exposes the columns WU-2's dashboard reads over HTTP. Nothing downstream works until
these surfaces exist.

**Do:**
1. Widen `SearchResultItem` (`api/routes.py:59`) to include `access_count`, `last_accessed`,
   `source_session`, `project_dir`. Confirm the full-result serializer `_search_result_to_dict`
   (`mcp/tools.py:71`) already emits them and that the REST `POST /api/v1/memories/search` path
   returns full parity with the MCP path (the summary path at L93 already carries these — this brings
   the *full* path and the REST model up to the same set).
2. Extend `get_stats` (`db/queries.py:334`) and `StatsResponse` (`api/routes.py:103`) with the audit's
   headline aggregates: `never_retrieved` (count where `access_count = 0`), `top_n_share` (retrieval
   concentration of the top-15), `unscoped` (count where `project_dir IS NULL`).
3. Add `memory_why(id)` — a thin read returning `source_session`, `created_at`, `access_count`,
   `last_accessed`, and `supersedes`/`consolidated_from` lineage. Expose as **both** an MCP tool (in
   `server.py` `list_tools`/`call_tool` + `mcp/tools.py`) and `GET /api/v1/memories/{id}/why`. No new
   storage — all fields exist.

**Adversarial hunt list (§6):** widened field silently returns `null`/omitted (a no-op surfacing);
parity gap between the full and `summary_only` serializers; `never_retrieved`/`top_n_share` off-by-one
or wrong denominator; `memory_why` returning content it shouldn't, or leaking PII via `source_session`
into a response that gets logged.

**Verify:** a `memory_search` full result includes `access_count` + `source_session`; `GET /stats`
includes `never_retrieved`; `memory_why` returns lineage for a known id.

**Proof (paste output):**
```bash
pytest tests/ -q                        # all green, incl. new tests mirroring test_summary_only.py
# never_retrieved matches ground truth:
sqlite3 "$MEMORY_DB_PATH" "SELECT count(*) FROM memories WHERE access_count = 0;"
# then compare to the number GET /api/v1/stats reports (once WU-2 serves it; until then assert in a unit test)
```

---

## WU-2 · A1 — Read-only memory dashboard  ·  MED risk  ·  starts after WU-1 merged

**Tracking issue:** *"A1: read-only memory dashboard + REST app"*, labels `enhancement`,
`observability`. Body references design doc §A1 and notes hosting **option (a)** is chosen.

**Do:**
1. **Stand up the REST app (the missing piece).** Add `src/claude_memory/api/app.py` with
   `create_app() -> FastAPI` that does `app.include_router(routes.router)` and mounts static assets.
   Host it as a **separate uvicorn service** — add a `--transport api` (or a dedicated console entry)
   and a **second docker-compose service** on its own port. **Do not** mount into `server.py` (§2).
2. **Add a list endpoint** — `GET /api/v1/memories` with `sort` (`access_count` | `importance` |
   `created_at`), `order`, `filter` (`never_retrieved` | `unscoped` | `tier=` | `type=`), `limit`,
   `offset`. Back it with a new `list_memories(...)` in `db/queries.py`: plain SELECT + ORDER BY +
   LIMIT/OFFSET; `never_retrieved = WHERE access_count = 0`; `unscoped = WHERE project_dir IS NULL`.
   **`sort`/`order`/`filter` MUST be whitelist-mapped to fixed column names** — never string-interpolate
   user input into SQL.
3. **Static dashboard** — one self-contained `dashboard/index.html` (no build step) served by the app:
   sortable table, preset filter chips (never-retrieved / top-15 / unscoped), per-row `forget`
   (archive) and `importance` edit wired to the existing `DELETE`/`PATCH /memories/{id}` routes, a
   header reading the extended `/stats`. Read-mostly; **confirm dialog on every mutation**.

**Adversarial hunt list (§6):** the new app importing or mounting into the MCP path (breaks §2);
**SQL injection via `sort`/`filter`/`order`** (must be whitelisted, not interpolated); a `forget` that
**hard-deletes** instead of archiving; the list defaulting to **all projects** (unscoped) and leaking
cross-project rows; the mutation surface bound to `0.0.0.0` with no confirm guard; the dashboard app
process interfering with the MCP process (shared port/db-lock contention).

**Verify:** app serves the page; the never-retrieved chip count equals the SQL ground truth; a UI
`forget` archives the row (tier → archived) and it leaves the default view; **MCP stdio/sse behavior
is unchanged** (the MCP service still starts and answers).

**Proof (paste output):**
```bash
pytest tests/ -q
# start the dashboard app, then:
curl -s "http://localhost:<api-port>/api/v1/stats" | grep never_retrieved
curl -s "http://localhost:<api-port>/api/v1/memories?filter=never_retrieved&limit=5"
sqlite3 "$MEMORY_DB_PATH" "SELECT count(*) FROM memories WHERE access_count = 0;"   # equals stats.never_retrieved
# confirm the MCP service is untouched: it still starts on 8787 and lists tools
```

---

## WU-3 · A3 — Contradiction-reconciliation in consolidate  ·  MED risk (destructive-adjacent)  ·  starts after WU-2 merged

**Tracking issue:** *"A3: contradiction-reconciliation in consolidate (human-confirmed)"*, labels
`enhancement`, `hygiene`. Body references design doc §A3 and restates: **no auto-supersede; human
confirm required.**

**Do:**
- Extend `lifecycle/consolidation.py`: reuse `find_clusters` (cosine 0.75) to detect high-similarity
  pairs, then split behavior:
  - **near-identical** → the existing summary/merge path (`run_consolidation`).
  - **conflicting** (high similarity + diverging `updated_at`/importance) → set the **newer** as
    `supersedes` the **older** and archive the older (`tier='archived'`). Never hard-delete; the
    pointer graph supports this via `supersedes` + `update_tiers`.
- Surface reconciliation **candidates** in the WU-2 dashboard for **human confirm before any
  supersede** — do not auto-supersede silently.

**Scope discipline:** this is corpus *hygiene*, **not** rule-enforcement. Reconciling prose did not
stop rule recurrence (w31rd #1553 evidence); recurring-rule enforcement is #1553's deterministic
gates (M1), out of scope here.

**Adversarial hunt list (§6):** auto-supersede firing without human confirm (the plan forbids it);
hard-delete instead of supersede+archive; false-positive clustering merging genuinely *different*
memories; lost lineage (`supersedes`/`consolidated_from` left unset so provenance breaks); archiving
the **newer** instead of the older (direction inverted).

**Verify:** a seeded contradictory pair (same topic, diverging `updated_at`) is flagged as a
candidate; confirming it sets `supersedes` on the newer + archives the older; the older stops
appearing in default search; lineage resolves via `memory_why` (WU-1).

**Proof (paste output):**
```bash
pytest tests/ -q                        # incl. a seeded-contradiction reconciliation test
# older memory no longer in default search, lineage intact:
sqlite3 "$MEMORY_DB_PATH" "SELECT id, tier, supersedes FROM memories WHERE id IN ('<older>','<newer>');"
```

---

## 9. Self-check before this file ships (author-time, not per-WU)

- Every Key-Files path resolves; every WU's Proof is a runnable command with an expected result — a WU
  whose "done" is prose, not a check, is a defect.
- No `auto-extract` / `profile` / `unscoped cross-project` / `mount into server.py` language survives
  except as an explicit **do-not** in §2.
- Commit/PR authorization is scoped to Tier A only (§4); the executor commits + opens PRs but never
  merges — the merge is Brandon's manual gate.
- Illustrative "bad" snippets (e.g. an interpolated-SQL example) live inside fenced code blocks.

---
*Basis: Tier A design doc (2026-08-07) + j5 codebase read 2026-08-07. Shape per j5
`01KYEBQAASEMYHRS2YPF62AGBG` (single-orchestrator model). Aligns under w31rd #1553.*
