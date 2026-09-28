#!/usr/bin/env bash
# session-start-recall.sh
# Command-type SessionStart hook. Replaces the old advisory session-start-memory.sh.
#
# Behavior:
#   - Queries johnny-five for: the latest session-state memory, top lessons,
#     user preferences, recent project memories (all scoped to current project_dir).
#   - Formats them into a Markdown block.
#   - Emits as hookSpecificOutput.additionalContext so Claude sees it as
#     part of session-start context — no tool call required from the model.
#
# Rationale: the old approach depended on the model remembering to call
# memory_recall. This script does it mechanically, so recall happens even
# when the model is distracted or post-compaction.
#
# Invariant: stdout MUST be a single valid JSON object. Stderr is free-form.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/j5-runtime.sh
source "$SCRIPT_DIR/lib/j5-runtime.sh"

j5_load_payload
parsed="$(j5_payload_fields cwd session_id)"
IFS=$'\x1f' read -r PAYLOAD_CWD SID <<< "$parsed"
CWD="$(j5_project_cwd "$PAYLOAD_CWD")"
export NB_CWD="$CWD"

# A linked worktree shares its scope, session-state included, with every other
# checkout of the repository. Name this session's own checkout so the reader
# can tell whether a recalled session-state is theirs. Git itself says whether
# this is a linked worktree: its own git dir differs from the common one. Git
# before 2.31 echoes --path-format back as a first line; treat that as unknown.
WORKTREE_INFO="$(j5_git -C "$(j5_session_cwd "$PAYLOAD_CWD")" rev-parse --path-format=absolute --git-dir --git-common-dir --show-toplevel 2>/dev/null)"
case "$WORKTREE_INFO" in --*) WORKTREE_INFO="" ;; esac
{ read -r OWN_GIT_DIR; read -r COMMON_GIT_DIR; read -r SESSION_ROOT; } <<< "$WORKTREE_INFO"
if [ -n "$SESSION_ROOT" ] && [ "$OWN_GIT_DIR" != "$COMMON_GIT_DIR" ]; then
  export NB_SESSION_ROOT="$SESSION_ROOT"
fi

if ! j5_require_canonical_container; then
  j5_emit_context "SessionStart" "session-start-recall: $J5_CONTAINER_DIAGNOSTIC. Restore the canonical SSE service, then start a fresh task to reload MCP tools."
  exit 0
fi

output="$(docker exec -i -e NB_CWD -e NB_SESSION_ROOT johnny-five python <<'PYEOF' 2>/dev/null
import asyncio, json, os, sys

def emit(context_str):
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context_str,
        }
    }))
    sys.stdout.flush()

async def main():
    cwd = os.environ.get('NB_CWD', '') or ''
    session_root = os.environ.get('NB_SESSION_ROOT', '') or ''
    try:
        from claude_memory.mcp import tools
    except Exception as e:
        emit(f"session-start-recall: could not import claude_memory ({e}). Call memory_recall manually if needed.")
        return

    try:
        result = await tools.tool_memory_recall(
            project_dir=cwd,
            initial_context='session start resume context',
            top_k=15,
        )
    except Exception as e:
        emit(f"session-start-recall: memory_recall failed ({e}). Call it manually with project_dir='{cwd}'.")
        return

    results = result.get('results') or []
    if not results:
        emit(f"session-start-recall: johnny-five reachable but no memories for project_dir={cwd!r} yet. Store insights as you learn them.")
        return

    # Categorise top memories by role.
    session_state = None
    lessons = []
    preferences = []
    projects = []
    for r in results:
        tags = r.get('tags') or []
        t = r.get('type', '')
        if not session_state and ('session-state' in tags or 'precompact' in tags):
            session_state = r
        elif t == 'lesson' and len(lessons) < 5:
            lessons.append(r)
        elif t in ('user', 'feedback') and len(preferences) < 3:
            preferences.append(r)
        elif t == 'project' and len(projects) < 3 and r is not session_state:
            projects.append(r)

    lines = ["# Resume Context (auto-recalled by session-start-recall hook)", ""]
    lines.append(f"Scoped to `project_dir={cwd}`. {len(results)} memories loaded.")
    if session_root:
        lines.append(f"This session runs in the git worktree `{session_root}`, which shares this project's memories; pass the project_dir above on manual memory calls.")

    if session_state:
        created = (session_state.get('created_at') or '?')[:19].replace('T', ' ')
        content = session_state.get('content') or ''
        preview = content[:800]
        if len(content) > 800:
            preview += "\n... [truncated; full content via memory_recall tag=session-state]"
        lines.append("")
        lines.append(f"## Last session-state  ({created} UTC, importance {session_state.get('importance', '?')})")
        lines.append("```")
        lines.append(preview)
        lines.append("```")
        floor_tag = 'mechanical-floor' in (session_state.get('tags') or [])
        if floor_tag:
            lines.append("")
            lines.append("> NOTE: this session-state was written by precompact-enforce (mechanical floor), not by the model. Inspect git log/status and any open plan file to reconstruct richer context.")
        if session_root:
            lines.append("")
            lines.append("> NOTE: every checkout of this project shares one session-state, so this one may come from another worktree. Compare its branch and cwd with this session before resuming from it.")

    if projects:
        lines.append("")
        lines.append("## Recent project memories")
        for p in projects:
            preview = (p.get('content') or '')[:240].replace('\n', ' ').strip()
            lines.append(f"- {preview}")

    if lessons:
        lines.append("")
        lines.append("## Top lessons for this project")
        for l in lessons:
            preview = (l.get('content') or '')[:220].replace('\n', ' ').strip()
            lines.append(f"- {preview}")

    if preferences:
        lines.append("")
        lines.append("## User preferences / feedback")
        for p in preferences:
            preview = (p.get('content') or '')[:180].replace('\n', ' ').strip()
            lines.append(f"- {preview}")

    lines.append("")
    lines.append("(memory_recall was auto-invoked by the SessionStart hook. Use memory_search for specific queries; avoid re-calling memory_recall unless you need different scope.)")

    emit('\n'.join(lines))

asyncio.run(main())
PYEOF
)"

exit_code=$?

if [ $exit_code -eq 0 ] && [ -n "$output" ]; then
  printf '%s' "$output"
else
  j5_emit_context "SessionStart" "session-start-recall: docker exec against the canonical johnny-five container failed. Restore the SSE service, then start a fresh task to reload MCP tools."
fi
