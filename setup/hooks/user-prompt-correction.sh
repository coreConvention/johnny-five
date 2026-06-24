#!/usr/bin/env bash
# user-prompt-correction.sh
# UserPromptSubmit hook. If the user's message contains a correction signal
# ("actually...", "no, that's wrong", "you already told me", etc.), auto-run
# memory_search against johnny-five and inject the top matches as
# additionalContext. Operationalises the "SEARCH ON CORRECTION" rule — stops
# the "you already told me this" failure pattern without depending on the model
# remembering to search.
#
# Design:
#   - Regex on the user prompt is cheap; only call docker exec on a match.
#   - Top-3 results, total content capped at 600 tokens via token_budget.
#   - Stderr suppressed; stdout must be a single valid JSON object (or empty
#     for no-op).
#   - Never blocks the user's message. Failures are silent.

set +e  # degrade gracefully; never block the user

PAYLOAD=$(cat)

# Parse prompt + cwd + session_id. Python is the portable JSON tool here.
parsed=$(printf '%s' "$PAYLOAD" | python -c "
import json, sys
try:
    d = json.load(sys.stdin)
    # Unit-separator (\x1f) lets us split safely even if prompt contains tabs/newlines.
    fields = [
        (d.get('prompt') or '')[:1000],
        d.get('cwd') or '',
        d.get('session_id') or '',
    ]
    print('\x1f'.join(fields))
except Exception:
    print('')
" 2>/dev/null)

if [ -z "$parsed" ]; then
    exit 0  # couldn't parse payload
fi

IFS=$'\x1f' read -r PROMPT CWD SID <<< "$parsed"

# Correction-signal regex. Case-insensitive. Cost balance: a false positive is
# one cheap memory_search; a false negative is the kind of "you already told me
# this" failure that costs trust and burns context. Lean inclusive.
#
# Three families of signals:
#   1. Hard corrections — explicit reversal of the model's last action
#      ("actually", "wrong", "incorrect", "you forgot", "no, that")
#   2. Soft corrections — language that implies "what you did wasn't what I
#      wanted" without naming it as an error ("does not match",
#      "i wanted/meant/needed", "should have", "supposed to",
#      "has nothing to do with", "not what i", "stop doing")
#   3. Method-questioning — implicit correction by interrogating the approach
#      ("why is(n't)", "are you using/doing/sure", "have you")
#
# All three families fire, e.g.: "this does not match the size", "i wanted this
# integrated into", "why is it not rendering", "are you using J5 at all".
if ! printf '%s' "$PROMPT" | grep -qiE '\b(actually[[:space:]]|wrong[[:space:],]|incorrect|course[- ]?correct|already[[:space:]]+(told|said|mentioned)|you[[:space:]]+forgot|that.?s[[:space:]]+not[[:space:]]+(right|true)|no,?[[:space:]]+(that|you)|does[[:space:]]?n.?t[[:space:]]+match|doesn.?t[[:space:]]+work|i[[:space:]]+(wanted|meant|need(ed)?)|why[[:space:]]+(is|isn.?t|are|aren.?t|did|did[[:space:]]?n.?t)|should[[:space:]]+(have|n.?t)|supposed[[:space:]]+to|has[[:space:]]+nothing[[:space:]]+to[[:space:]]+do|are[[:space:]]+you[[:space:]]+(using|doing|even|sure|going)|have[[:space:]]+you|stop[[:space:]]+(doing|making|asking|trying)|not[[:space:]]+what[[:space:]]+i|that.?s[[:space:]]+not[[:space:]]+what|(it.?s|is|its)[[:space:]]+not[[:space:]]+(floating|rendering|working|matching|aligned|right|correct|here|there|how|what)|(taller|shorter|bigger|smaller|wider|narrower|larger|broken)[[:space:]]+(than|now))\b'; then
    exit 0  # not a correction — silent no-op
fi

# Mark this turn as having seen a correction. Pairs with
# memory-discipline-enforce.js's Stop-block: the model can't end a turn where
# the user corrected it AND no memory_search happened. Best-effort — silent on
# failure to avoid breaking the user prompt path.
if [ -n "$SID" ]; then
    DISCIPLINE_DIR="$HOME/.claude/hooks/state"
    DISCIPLINE_FILE="$DISCIPLINE_DIR/memory-discipline-$SID.json"
    mkdir -p "$DISCIPLINE_DIR" 2>/dev/null
    python -c "
import json, sys, os
path = sys.argv[1]
try:
    state = {}
    if os.path.exists(path):
        with open(path) as f:
            state = json.load(f) or {}
    state['correction_seen'] = True
    # Reset searches counter when a NEW correction fires, so the Stop-block
    # is gated on 'searches since this correction', not lifetime.
    state['searches_at_correction'] = state.get('searches', 0)
    with open(path, 'w') as f:
        json.dump(state, f)
except Exception:
    pass
" "$DISCIPLINE_FILE" 2>/dev/null
fi

# Discover a running johnny-five container (compose name first, then bare).
J5_CONTAINER=""
RUNNING="$(docker ps --filter "name=johnny-five" --format "{{.Names}}" 2>/dev/null)"
for candidate in johnny-five-johnny-five-1 johnny-five; do
    if printf '%s\n' "$RUNNING" | grep -qx "$candidate"; then
        J5_CONTAINER="$candidate"
        break
    fi
done
[ -z "$J5_CONTAINER" ] && exit 0  # no container — silent (correction state already recorded above)

export NB_CWD="$CWD"
export NB_QUERY="$(printf '%s' "$PROMPT" | head -c 300)"

output="$(docker exec -i -e NB_CWD -e NB_QUERY "$J5_CONTAINER" python - <<'PYEOF' 2>/dev/null
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
