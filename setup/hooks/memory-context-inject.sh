#!/usr/bin/env bash
# memory-context-inject.sh   (Tier 4 — enforced search-first, PreToolUse)
#
# Before an Edit/Write on a "risky" code path, or a curated risky Bash command,
# this hook runs `memory_search` against johnny-five and injects the top hits as
# additionalContext — so prior lessons appear BEFORE the model acts on the file
# or runs the command, instead of relying on the model to remember to search.
#
# Pairs with user-prompt-correction.sh (the UserPromptSubmit-side search).
# Together they cover both ends: corrections trigger a search after a user
# message; this hook triggers a search before tool use.
#
# >>> CUSTOMIZE ME <<<
#   The two trigger sections below (path glob + Bash command regexes) are
#   DELIBERATELY GENERIC EXAMPLES. Tune them to YOUR codebase's footguns — the
#   files where a forgotten convention bites, and the commands that have burned
#   you before. Over-broad triggers add noise; that's why it fires on a narrow,
#   curated set rather than every edit. Trivial edits (docs/, .tmp/) should NOT
#   trigger.
#
# Design:
#   - 5-minute per-key cache to avoid re-firing on consecutive edits to the
#     same file. State lives below the deployed hook directory.
#   - Top-2 results, total content capped at 400 tokens via token_budget.
#   - Score gate (>=0.55) so only meaningfully-relevant hits are injected.
#   - Never blocks. Failures are silent.

set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/j5-runtime.sh
source "$SCRIPT_DIR/lib/j5-runtime.sh"

# ---------------------------------------------------------------------------
# Read payload
# ---------------------------------------------------------------------------
j5_load_payload
parsed="$(j5_payload_fields tool_name tool_input.file_path tool_input.command cwd session_id)"

if [ -z "$parsed" ]; then
    exit 0
fi

IFS=$'\x1f' read -r TOOL FILE_PATH CMD CWD SID <<< "$parsed"
CWD="$(j5_project_cwd "$CWD")"
SID="${SID:-unknown}"
CMD="$(printf '%s' "$CMD" | head -c 500)"

# ---------------------------------------------------------------------------
# Decide whether to fire — and what to search for.
# >>> CUSTOMIZE the path glob and the Bash command patterns below. <<<
# ---------------------------------------------------------------------------
QUERY=""
KEY=""

case "$TOOL" in
    Edit|Write)
        # EXAMPLE: fire on source files under common source roots. Replace with
        # the paths in YOUR repo where a forgotten convention actually bites.
        if printf '%s' "$FILE_PATH" | grep -qiE '/(src|lib|libs|app|apps)/.*\.(ts|tsx|js|jsx|py|go|rs|cs|java|rb)$'; then
            # Search on the file's basename (no extension) plus its parent dir
            # name — gives both symbol-name and component-kind context.
            basename=$(basename "$FILE_PATH" | sed 's/\.[^.]*$//')
            parent=$(basename "$(dirname "$FILE_PATH")")
            QUERY="$basename $parent"
            KEY="edit:$FILE_PATH"
        fi
        ;;
    apply_patch)
        if printf '%s' "$CMD" | grep -qiE '(src|lib|libs|app|apps)[/\\].*\.(ts|tsx|js|jsx|py|go|rs|cs|java|rb)'; then
            FILE_PATH="$(printf '%s' "$CMD" | grep -oiE '(src|lib|libs|app|apps)[/\\][^[:space:]]+\.(ts|tsx|js|jsx|py|go|rs|cs|java|rb)' | head -1)"
            basename="$(basename "$FILE_PATH" | sed 's/\.[^.]*$//')"
            parent="$(basename "$(dirname "$FILE_PATH")")"
            QUERY="$basename $parent"
            KEY="edit:$FILE_PATH"
        fi
        ;;
    Bash)
        # EXAMPLE: a few commands that commonly have project-specific gotchas.
        # Add the keyword that should trigger the search, and a short query.
        if printf '%s' "$CMD" | grep -qiE '(^|[[:space:]])git[[:space:]]+worktree[[:space:]]+add\b'; then
            QUERY="git worktree naming convention setup"
            KEY="bash:git-worktree"
        elif printf '%s' "$CMD" | grep -qiE '(^|[[:space:]])npm[[:space:]]+(install|ci)\b'; then
            QUERY="npm install peer-deps lockfile gotchas"
            KEY="bash:npm-install"
        elif printf '%s' "$CMD" | grep -qiE '(^|[[:space:]])docker[[:space:]]+(compose|run|exec)\b'; then
            QUERY="docker container lifecycle volume gotchas"
            KEY="bash:docker"
        fi
        ;;
esac

if [ -z "$QUERY" ]; then
    exit 0  # not a target tool/path/command — silent no-op
fi

# ---------------------------------------------------------------------------
# Require the one canonical Johnny-Five container.
# ---------------------------------------------------------------------------
if ! j5_require_canonical_container; then
    j5_emit_context "PreToolUse" "memory-context-inject: $J5_CONTAINER_DIAGNOSTIC; no automatic search was run. Restore SSE and start a fresh task to reload MCP tools."
    exit 0
fi

# ---------------------------------------------------------------------------
# Per-key cache — skip if we already searched this key recently
# ---------------------------------------------------------------------------
CACHE_DIR="$(j5_state_dir)"
mkdir -p "$CACHE_DIR" 2>/dev/null
CACHE_FILE="$CACHE_DIR/memory-context-cache-$SID.json"
TTL_SEC=300  # 5 minutes — long enough to suppress consecutive edits, short
             # enough that a returning task gets fresh context

NOW=$(date +%s)

if [ -f "$CACHE_FILE" ]; then
    last_seen=$(python -c "
import json, sys
try:
    with open(sys.argv[1]) as f:
        c = json.load(f)
    print(c.get(sys.argv[2], 0))
except Exception:
    print(0)
" "$CACHE_FILE" "$KEY" 2>/dev/null)

    if [ -n "$last_seen" ] && [ "$last_seen" != "0" ]; then
        age=$((NOW - last_seen))
        if [ "$age" -lt "$TTL_SEC" ]; then
            exit 0  # within TTL — skip duplicate search
        fi
    fi
fi

# Update cache (best-effort)
python -c "
import json, sys, os
path = sys.argv[1]
key = sys.argv[2]
now = int(sys.argv[3])
try:
    if os.path.exists(path):
        with open(path) as f:
            c = json.load(f)
    else:
        c = {}
    c[key] = now
    # Prune entries older than 7d
    cutoff = now - 7*86400
    c = {k: v for k, v in c.items() if v >= cutoff}
    with open(path, 'w') as f:
        json.dump(c, f)
except Exception:
    pass
" "$CACHE_FILE" "$KEY" "$NOW" 2>/dev/null

# ---------------------------------------------------------------------------
# Search johnny-five and emit additionalContext
# ---------------------------------------------------------------------------
export NB_CWD="$CWD"
export NB_QUERY="$QUERY"
export NB_KEY="$KEY"

output="$(docker exec -i -e NB_CWD -e NB_QUERY -e NB_KEY johnny-five python - <<'PYEOF' 2>/dev/null
import asyncio, json, os, sys

def emit(context_str):
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context_str,
        }
    }))
    sys.stdout.flush()

async def main():
    cwd = os.environ.get('NB_CWD') or ''
    query = os.environ.get('NB_QUERY') or ''
    key = os.environ.get('NB_KEY') or ''
    try:
        from claude_memory.mcp.tools import tool_memory_search
    except Exception:
        return  # silent on import failure

    try:
        result = await tool_memory_search(
            query=query,
            project_dir=cwd,
            top_k=2,
            token_budget=400,
        )
    except Exception:
        return  # silent on search failure

    hits = result.get('results') or []
    # Score gate: only inject if at least one hit is meaningfully relevant.
    # Below 0.55 the hits tend to be tangential and add noise.
    relevant = [h for h in hits if h.get('score', 0) >= 0.55]
    if not relevant:
        return

    lines = [f"# memory-context-inject (key={key})"]
    lines.append(f"Searched J5 for: {query!r}")
    for h in relevant[:2]:
        preview = (h.get('content') or '')[:280].replace('\n', ' ').strip()
        score = h.get('score', 0)
        mid = h.get('id', '')
        lines.append(f"- ({mid}, score={score:.2f}) {preview}")
    lines.append("")
    lines.append(
        "(Auto-injected because the tool target matched a known-gotcha pattern. "
        "Skim before continuing — if these aren't relevant, ignore.)"
    )
    emit('\n'.join(lines))

asyncio.run(main())
PYEOF
)"

exit_code=$?

if [ $exit_code -eq 0 ] && [ -n "$output" ]; then
    printf '%s' "$output"
fi
exit 0
