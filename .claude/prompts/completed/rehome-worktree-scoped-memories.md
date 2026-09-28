# Re-home worktree-scoped memories (issue #38): execution prompt

> **Executed 2026-09-28, after #39 merged:** 196 moved, 0 skipped, 0 failed,
> 31 session snapshots left in place, 0 worktree-scoped rows left. #38 is
> closed. A re-run is safe: the plan comes back empty.

> **Task:** finish the data half of [`coreConvention/johnny-five#38`](https://github.com/coreConvention/johnny-five/issues/38).
> Hooks older than #38 scoped every git-worktree session to the worktree's own
> path, so memories stored from worktrees sit under paths no later session asks
> for. Move them, in place and keeping their ids, to their repository's main
> checkout with `memory_update(project_dir=...)`.
> **The global commit gate holds:** this task changes data, not code. Commit
> nothing unless the owner says so in the moment.

**Owner decision (recorded 2026-09-28, in the session that wrote the fix):**
move every non-snapshot worktree-scoped memory to its main checkout with J5's own
`memory_update`; session snapshots stay put. The census then found **196** to move
(195 to `Z:/Personal/w31rd.com`, 1 to `Z:/Personal/johnny-five`) and 31 snapshots
to keep. Proceed without asking again **only** if the new plan reports 0 problems
and a move count within about 10% of 196. Otherwise show the owner the plan
summary and ask first.

**Binding refs:** issue #38 and its findings comment; the PR that shipped its code half;
johnny-five memory `01M3MTTZ97A84R4ATZD69HRGR4` (diagnosis and resolution); the
rebuild runbook `01M3MEY1FKMGQ991E4MSBPQCFF` (it is scoped to
`Z:/Personal/w31rd.com`, so search there or unscoped).

---

## 1. Preconditions (stop and report if any fails)

1. The PR with the #38 code change is merged, and the primary checkout `Z:/Personal/johnny-five`
   is on `main` and fast-forwarded (`git pull --ff-only origin main`).
2. The container image was rebuilt from that `main` per the runbook, **including its
   DB snapshot step**, which doubles as this task's backup. Recreate **both**
   services: the moves go through the dashboard's REST `PATCH`, and the runbook's
   usual `up -d --no-deps johnny-five` leaves `johnny-five-dashboard` on the old image.
3. The dashboard answers `GET http://127.0.0.1:8788/api/v1/stats`.
4. After any J5 restart the owner must reconnect the Claude desktop app (see the
   runbook's tripwire). Ask them to, rather than debugging the server.

**Hard rules:** talk to J5 only through its own surfaces (this script uses the
dashboard REST API). Never open `/data/memory.db` from another process. Never
hard-delete anything.

## 2. Run it

Write the script below to your scratchpad, then:

```bash
python rehome_worktree_memories.py plan plan.json        # read-only; prints the summary
python rehome_worktree_memories.py apply plan.json log.jsonl
python rehome_worktree_memories.py verify                # expect: 0 rows left
```

`apply` re-reads each memory first and skips any whose scope changed since the
plan, so a re-run is safe. If the server does not accept `project_dir` yet, it
stops at the first memory without moving anything: go back to precondition 2.

Spot-check afterwards: `memory_search` with `project_dir=Z:/Personal/w31rd.com`
for the topic of two moved memories should now return them.

## 3. Rollback

Each moved memory keeps its old scope in `metadata.previous_project_dirs`, and
`log.jsonl` has every `from` and `to`. To undo, `PATCH` each logged id back to
its `from`.

## 4. Close out

- Comment on #38 with the counts moved, skipped and left. Use counts only, with
  no personal names, emails or user-profile paths. Then close #38: its code half
  shipped in the PR, and this move is its data half.
- Update memory `01M3MTTZ97A84R4ATZD69HRGR4` with the outcome.
- Move this prompt to `.claude/prompts/completed/` in the next docs change.

## The script

```python
"""Move worktree-scoped Johnny-Five memories to their main checkout (issue #38).

Talks only to the running dashboard REST API; it never opens the database.
  plan   PLAN.json            read-only census, mapping and summary
  apply  PLAN.json LOG.jsonl  move each planned memory, re-checking it first
  verify                      read-only: list worktree-scoped rows still left

Mapping, by the owner's worktree conventions:
  <drive>:/Worktrees/<repo>/<name>          -> <drive>:/Personal/<repo>
  <main checkout>/.claude/worktrees/<name>  -> <main checkout>
A worktree still on disk is cross-checked against git's own answer. Session
snapshots (session-state / precompact / mechanical-floor tags) never move:
in the shared scope they would pose as another session's latest state.
"""

from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

API = os.environ.get("J5_DASHBOARD_API", "http://127.0.0.1:8788/api/v1")
PAGE = 500
SNAPSHOT_TAGS = {"session-state", "precompact", "mechanical-floor"}
DESKTOP = re.compile(r"^(?P<drive>[A-Za-z]):[\\/]+Worktrees[\\/]+(?P<repo>[^\\/]+)[\\/]+[^\\/]+[\\/]*$", re.IGNORECASE)
IN_REPO = re.compile(r"^(?P<main>.+?)[\\/]+\.claude[\\/]+worktrees[\\/]+[^\\/]+[\\/]*$", re.IGNORECASE)


def request(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def all_rows() -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        status, page = request("GET", f"/memories?include_archived=true&limit={PAGE}&offset={offset}&sort=created_at&order=asc")
        if status != 200:
            sys.exit(f"list failed: HTTP {status} {page}")
        rows.extend(page["results"])
        if len(page["results"]) < PAGE:
            return rows
        offset += PAGE


def target_for(project_dir: str) -> str | None:
    match = DESKTOP.match(project_dir)
    if match:
        return f"{match['drive'].upper()}:/Personal/{match['repo']}"
    match = IN_REPO.match(project_dir)
    if match:
        return match["main"].replace("\\", "/")
    return None


def git_main_checkout(directory: str) -> str | None:
    if not os.path.isdir(directory):
        return None
    result = subprocess.run(
        ["git", "-C", directory, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, check=False,
    )
    common = result.stdout.strip()
    return common[: -len("/.git")] if result.returncode == 0 and common.endswith("/.git") else None


def same_dir(left: str, right: str) -> bool:
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def worktree_scoped(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row.get("project_dir") and target_for(row["project_dir"])]


def plan(plan_path: str) -> None:
    rows = all_rows()
    candidates = worktree_scoped(rows)
    moves: list[dict] = []
    problems: list[str] = []
    snapshots = 0
    for row in candidates:
        if SNAPSHOT_TAGS & set(row["tags"]):
            snapshots += 1
            continue
        target = target_for(row["project_dir"])
        truth = git_main_checkout(row["project_dir"])
        if not os.path.isdir(target):
            problems.append(f"{row['id']}: target {target} does not exist")
        elif truth is not None and not same_dir(truth, target):
            problems.append(f"{row['id']}: convention says {target}, git says {truth}")
        else:
            moves.append({"id": row["id"], "from": row["project_dir"], "to": target, "verified_by_git": truth is not None})
    json.dump(moves, open(plan_path, "w", encoding="utf-8"), indent=2)
    print(f"rows: {len(rows)} | worktree-scoped: {len(candidates)} | snapshots kept: {snapshots} | planned moves: {len(moves)}")
    for target, count in collections.Counter(move["to"] for move in moves).most_common():
        print(f"  {count:>4} -> {target}")
    print(f"  cross-checked by git: {sum(move['verified_by_git'] for move in moves)}")
    print(f"problems: {len(problems)}")
    for problem in problems:
        print(f"  {problem}")


def apply(plan_path: str, log_path: str) -> None:
    moves = json.load(open(plan_path, encoding="utf-8"))
    outcomes: collections.Counter[str] = collections.Counter()
    with open(log_path, "a", encoding="utf-8") as log:
        for move in moves:
            status, current = request("GET", f"/memories/{move['id']}")
            if status != 200 or current.get("project_dir") != move["from"]:
                outcome = "skipped: missing or changed since the plan"
            else:
                status, result = request("PATCH", f"/memories/{move['id']}", {"project_dir": move["to"]})
                if status == 400 and "No fields" in json.dumps(result):
                    sys.exit("server does not accept project_dir yet: deploy the #38 server change first. Nothing was moved.")
                _, after = request("GET", f"/memories/{move['id']}")
                ok = status == 200 and result.get("updated") is True and after.get("project_dir") == move["to"]
                outcome = "moved" if ok else f"failed: HTTP {status} {result}"
            outcomes[outcome.split(":")[0]] += 1
            log.write(json.dumps({**move, "outcome": outcome}) + "\n")
    print(dict(outcomes))


def verify() -> None:
    left = [row for row in worktree_scoped(all_rows()) if not SNAPSHOT_TAGS & set(row["tags"])]
    print(f"non-snapshot worktree-scoped rows left: {len(left)}")
    for row in left:
        print(f"  {row['id']} {row['project_dir']}")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "plan" and len(sys.argv) == 3:
        plan(sys.argv[2])
    elif command == "apply" and len(sys.argv) == 4:
        apply(sys.argv[2], sys.argv[3])
    elif command == "verify" and len(sys.argv) == 2:
        verify()
    else:
        sys.exit(__doc__)
```
