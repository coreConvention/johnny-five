#!/usr/bin/env bash
# session-start-recall.sh
# Command-type SessionStart hook. Replaces the old advisory session-start-memory.sh.
#
# Behavior:
#   - Queries johnny-five for: the latest session-state memory, top lessons,
#     user preferences, recent project memories (all scoped to current project_dir).
#   - Formats them into a Markdown block.
#   - Emits as hookSpecificOutput.additionalContext so Claude sees it as
#     part of session-start context \u2014 no tool call required from the model.
#
# Rationale: the old approach depended on the model remembering to call
# memory_recall. This script does it mechanically, so recall happens even
# when the model is distracted or post-compaction.
#
# Invariant: stdout MUST be a single valid JSON object. Stderr is free-form.

CWD="${CLAUDE_PROJECT_DIR:-$(pwd)}"
export NB_CWD="$CWD"

output="$(docker exec -i -e NB_CWD johnny-five python <<'PYEOF' 2>/dev/null
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
  printf '%s' '{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "session-start-recall: johnny-five container unreachable. Start it: docker start johnny-five. Then call memory_recall manually."}}'
fi
