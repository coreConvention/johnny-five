#!/usr/bin/env bash
# precompact-enforce.sh
# Command-type PreCompact hook. Fires AFTER the prompt-type hook that
# instructs Claude to save session-state.
#
# Behavior:
#   - Queries johnny-five for a session-state memory <120s old for this project.
#   - If found: allow compaction, no action.
#   - If missing: write a mechanical floor summary (branch, cwd, recent files)
#     so the next session has SOMETHING to resume from. Never blocks compaction.
#
# Rationale: blocking risks user lockout if johnny-five is down or the model's
# save is slow. Mechanical floor captures ~80% of the value; model-authored
# rich summary remains preferred when it happens.
#
# Invariant: stdout MUST be a single valid JSON object. Stderr is free-form.

CWD="${CLAUDE_PROJECT_DIR:-$(pwd)}"
export NB_CWD="$CWD"

# Collect cheap git context (runs on host, not in container)
export NB_BRANCH="$(cd "$CWD" 2>/dev/null && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
export NB_RECENT="$(cd "$CWD" 2>/dev/null && git status --porcelain 2>/dev/null | head -10 | tr '\n' '|' || echo '')"

# Ask johnny-five: do we already have a recent session-state? If not, write a floor.
# Python inside the container always emits valid JSON on stdout (try/except-wrapped).
output="$(docker exec -i -e NB_CWD -e NB_BRANCH -e NB_RECENT johnny-five python <<'PYEOF' 2>/dev/null
import asyncio, json, os, sys
from datetime import datetime, timedelta, timezone

def emit(payload):
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()

async def main():
    cwd = os.environ.get('NB_CWD', '') or ''
    branch = os.environ.get('NB_BRANCH', 'unknown')
    recent = os.environ.get('NB_RECENT', '')
    try:
        from claude_memory.mcp import tools
    except Exception as e:
        emit({"continue": True, "hookSpecificOutput": {"hookEventName": "PreCompact", "additionalContext": f"precompact-enforce: could not import claude_memory ({e}). Compaction proceeds without enforcement."}})
        return

    # Check for a session-state memory stored in the last 120 seconds
    recent_save_found = False
    try:
        recall = await tools.tool_memory_recall(
            project_dir=cwd,
            initial_context='session-state precompact',
            top_k=10,
        )
        now = datetime.now(timezone.utc)
        for r in recall.get('results', []) or []:
            tags = r.get('tags') or []
            if 'session-state' in tags and 'precompact' in tags:
                ca = r.get('created_at') or r.get('updated_at') or ''
                try:
                    ts = datetime.fromisoformat(ca.replace('Z', '+00:00'))
                    if now - ts < timedelta(seconds=120):
                        recent_save_found = True
                        break
                except Exception:
                    continue
    except Exception as e:
        # Recall failed — proceed to write floor as if nothing was saved.
        emit({"continue": True, "hookSpecificOutput": {"hookEventName": "PreCompact", "additionalContext": f"precompact-enforce: recall check failed ({e}). Compaction proceeds; next session may lack state."}})
        return

    if recent_save_found:
        # Model already complied with the prompt-type hook. Nothing to do.
        emit({"continue": True})
        return

    # Write mechanical floor
    recent_files = [line for line in recent.split('|') if line.strip()]
    floor = {
        "prompt_file": None,
        "branch": branch,
        "cwd": cwd,
        "current_step": "MECHANICAL FLOOR \u2014 hook wrote this because model did not store a rich session-state before compaction",
        "completed_steps": [],
        "blockers": ["Model-authored summary missing; re-read this memory plus CLAUDE.md plus any open plan file to reconstruct context"],
        "runtime_ports": {},
        "active_decisions": [],
        "next_action": "Read CLAUDE.md, inspect git log/status, ask user for task context if unclear",
        "recent_git_status": recent_files,
        "written_by": "precompact-enforce.sh",
        "written_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await tools.tool_memory_store(
            content=json.dumps(floor, indent=2),
            type='project',
            tags=['session-state', 'precompact', 'mechanical-floor'],
            importance=7.0,
            project_dir=cwd,
            metadata={"floor": True, "branch": branch},
        )
        emit({
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "PreCompact",
                "additionalContext": "precompact-enforce wrote a mechanical session-state floor to johnny-five (model did not produce a rich summary). Next session's SessionStart hook will surface it.",
            },
        })
    except Exception as e:
        emit({
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "PreCompact",
                "additionalContext": f"precompact-enforce: store failed ({e}). Compaction proceeds without a floor.",
            },
        })

asyncio.run(main())
PYEOF
)"

exit_code=$?

if [ $exit_code -eq 0 ] && [ -n "$output" ]; then
  printf '%s' "$output"
else
  # Docker unreachable or container stopped
  printf '%s' '{"continue": true, "hookSpecificOutput": {"hookEventName": "PreCompact", "additionalContext": "precompact-enforce: johnny-five container unreachable. Compaction proceeds without enforcement. Start the container: docker start johnny-five"}}'
fi
