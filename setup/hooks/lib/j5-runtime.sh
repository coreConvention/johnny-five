#!/usr/bin/env bash

# Shared runtime primitives for hooks deployed under either Claude or Codex.
# Hook state intentionally follows the deployed script directory.

J5_HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
J5_STATE_DIR="$J5_HOOKS_DIR/state"
J5_PAYLOAD=""
J5_CONTAINER_DIAGNOSTIC=""

j5_load_payload() {
    J5_PAYLOAD="$(cat)"
}

j5_payload_fields() {
    printf '%s' "$J5_PAYLOAD" | python -c '
import json, sys

try:
    payload = json.load(sys.stdin)
    values = []
    for field in sys.argv[1:]:
        value = payload
        for part in field.split("."):
            value = value.get(part, "") if isinstance(value, dict) else ""
        if isinstance(value, (dict, list)):
            value = json.dumps(value, separators=(",", ":"))
        values.append(str(value or ""))
    print("\x1f".join(values))
except Exception:
    print("")
' "$@" 2>/dev/null
}

j5_project_cwd() {
    if [ -n "$1" ]; then
        printf '%s' "$1"
    else
        pwd
    fi
}

j5_state_dir() {
    mkdir -p "$J5_STATE_DIR" 2>/dev/null
    printf '%s' "$J5_STATE_DIR"
}

j5_require_canonical_container() {
    local running canonical_count j5_like_count
    running="$(docker ps --format "{{.Names}}" 2>/dev/null)"
    canonical_count="$(printf '%s\n' "$running" | grep -c '^johnny-five$' 2>/dev/null)"
    j5_like_count="$(printf '%s\n' "$running" | grep -Eic 'johnny[-_]five' 2>/dev/null)"

    if [ "$canonical_count" -eq 1 ] && [ "$j5_like_count" -eq 1 ]; then
        return 0
    fi

    if [ "$canonical_count" -eq 0 ] && [ "$j5_like_count" -eq 0 ]; then
        J5_CONTAINER_DIAGNOSTIC="canonical johnny-five container is not running"
    elif [ "$canonical_count" -eq 0 ]; then
        J5_CONTAINER_DIAGNOSTIC="a non-canonical Johnny-Five-like container is running; refusing to attach"
    else
        J5_CONTAINER_DIAGNOSTIC="multiple Johnny-Five-like containers are running; refusing to attach"
    fi
    return 1
}

j5_emit_context() {
    python -c '
import json, sys
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": sys.argv[1],
        "additionalContext": sys.argv[2],
    }
}), end="")
' "$1" "$2"
}
