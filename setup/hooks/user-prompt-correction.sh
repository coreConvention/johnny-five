#!/usr/bin/env bash
# user-prompt-correction.sh
# UserPromptSubmit hook. If the user's message contains a correction signal
# ("actually...", "no, that's wrong", "you already told me", etc.), auto-run
# memory_search against johnny-five and inject the top matches as
# additionalContext. Operationalises CLAUDE.md §3's "SEARCH ON CORRECTION"
# rule — stops the "you already told me this" failure pattern without
# depending on the model remembering to search.
#
# Design:
#   - Regex on user prompt is cheap; only call docker exec on a match.
#   - Top-3 results, total content capped at 600 tokens via token_budget.
#   - Stderr suppressed; stdout must be a single valid JSON object (or empty
#     for no-op).
#   - Never blocks the user's message. Failures are silent.

set +e  # degrade gracefully; never block the user

PAYLOAD=$(cat)

# Parse prompt + cwd. Python is the portable JSON tool available on this host.
parsed=$(printf '%s' "$PAYLOAD" | python -c "
import json, sys
try:
    d = json.load(sys.stdin)
    # Unit-separator (\x1f) lets us split safely even if prompt contains tabs/newlines.
    print((d.get('prompt') or '')[:1000] + '\x1f' + (d.get('cwd') or ''))
except Exception:
    print('')
" 2>/dev/null)

if [ -z "$parsed" ]; then
    exit 0  # couldn't parse payload
fi

PROMPT="${parsed%%$'\x1f'*}"
CWD="${parsed##*$'\x1f'}"

# Correction-signal regex. Case-insensitive. Intentionally conservative —
# false positives cost a cheap memory_search; false negatives cost trust.
if ! printf '%s' "$PROMPT" | grep -qiE '\b(actually[[:space:]]|wrong[[:space:],]|incorrect|course[- ]?correct|already[[:space:]]+(told|said|mentioned)|you[[:space:]]+forgot|that.?s[[:space:]]+not[[:space:]]+(right|true)|no,?[[:space:]]+(that|you))\b'; then
    exit 0  # not a correction — silent no-op
fi

export NB_CWD="$CWD"
export NB_QUERY="$(printf '%s' "$PROMPT" | head -c 300)"

output="$(docker exec -i -e NB_CWD -e NB_QUERY johnny-five python - <<'PYEOF' 2>/dev/null
import asyncio, json, os, sys

def emit(context_str):
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context_str,
        }
    }))
    sys.stdout.flush()

async def main():
    cwd = os.environ.get('NB_CWD', '') or ''
    query = os.environ.get('NB_QUERY', '') or ''
    try:
        from claude_memory.mcp.tools import tool_memory_search
    except Exception as e:
        emit(f"correction-signal hook: import failed ({e}); no auto-search this turn.")
        return

    try:
        result = await tool_memory_search(
            query=query,
            project_dir=cwd,
            top_k=3,
            token_budget=600,
        )
    except Exception as e:
        emit(f"correction-signal hook: search failed ({e}).")
        return

    hits = result.get('results') or []
    if not hits:
        emit(
            f"Correction signal detected in your last message. "
            f"memory_search found no prior lessons scoped to {cwd!r}."
        )
        return

    lines = ["# Correction-signal auto-search (possibly-relevant prior lessons)"]
    for h in hits:
        preview = (h.get('content') or '')[:220].replace('\n', ' ').strip()
        score = h.get('score', 0)
        lex = h.get('lexical_score', 0)
        lines.append(f"- (score={score:.2f}, lex={lex:.2f}) {preview}")
    lines.append("")
    lines.append(
        "(Triggered because your message matched a correction pattern. "
        "Advisory only — if these aren't the lessons you meant, ignore and continue.)"
    )
    emit('\n'.join(lines))

asyncio.run(main())
PYEOF
)"

exit_code=$?

if [ $exit_code -eq 0 ] && [ -n "$output" ]; then
    printf '%s' "$output"
fi
# Any other path: silent. Do not block user prompt submission.
