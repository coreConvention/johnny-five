#!/usr/bin/env bash

# Shared runtime primitives for hooks deployed under either Claude or Codex.
# Hook state intentionally follows the deployed script directory.

J5_HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
J5_STATE_DIR="$J5_HOOKS_DIR/state"
J5_PAYLOAD=""
J5_CONTAINER_DIAGNOSTIC=""

# Compose services that share the johnny-five name but are not memory servers,
# as a grep -E alternation. Keep in step with the container_name values in
# docker-compose.yml; tests/test_hook_contracts.py enforces that.
J5_KNOWN_SIBLINGS="johnny-five-dashboard"

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

# git for the repository that contains a given directory. An inherited GIT_DIR
# or GIT_WORK_TREE would make git ignore -C and answer for another repository.
j5_git() {
    (
        unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR
        git "$@"
    )
}

# The directory the session works in: the payload cwd when it names an
# existing directory, else the hook's own working directory. A malformed or
# stale payload must never become a memory scope.
j5_session_cwd() {
    if [ -n "$1" ] && [ -d "$1" ]; then
        printf '%s' "$1"
    else
        pwd
    fi
}

# The project_dir a hook scopes Johnny-Five calls to. The server matches scope
# by exact (case- and slash-folded) path, so every checkout of one repository
# has to report the same directory. Otherwise a session in a git worktree sees
# none of its repository's memories (issue #38). In order:
#   1. J5_PROJECT_DIR when set, verbatim. A separate clone has its own git
#      common dir, so this override is the only way to share a scope with it.
#   2. Inside a git repository, the main checkout: the parent of the absolute
#      git common dir. A linked worktree, the main checkout and any
#      subdirectory of either all resolve to the same answer.
#   3. Otherwise the session directory itself.
j5_project_cwd() {
    if [ -n "${J5_PROJECT_DIR:-}" ]; then
        printf '%s' "$J5_PROJECT_DIR"
        return 0
    fi

    local session_dir common_dir main_checkout
    session_dir="$(j5_session_cwd "$1")"
    # Git before 2.31 does not know --path-format and echoes it back as an
    # extra line, which the single-line check below rejects.
    common_dir="$(j5_git -C "$session_dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
    case "$common_dir" in
        *$'\n'*) ;;
        */.git | *\\.git)
            main_checkout="${common_dir%?.git}"
            if [ -d "$main_checkout" ]; then
                printf '%s' "$main_checkout"
                return 0
            fi
            ;;
    esac
    printf '%s' "$session_dir"
}

j5_state_dir() {
    mkdir -p "$J5_STATE_DIR" 2>/dev/null
    printf '%s' "$J5_STATE_DIR"
}

j5_require_canonical_container() {
    local running j5_like canonical_count j5_like_count
    running="$(docker ps --format "{{.Names}}" 2>/dev/null)"
    # The hazard is a second memory server on the same database, such as a
    # WSL-spawned orphan beside the compose service. Known non-server siblings
    # like the dashboard are not that hazard, so they never count (#35).
    j5_like="$(printf '%s\n' "$running" | grep -Evx "$J5_KNOWN_SIBLINGS" | grep -Ei 'johnny[-_]five')"
    canonical_count="$(printf '%s\n' "$j5_like" | grep -c '^johnny-five$')"
    j5_like_count="$(printf '%s\n' "$j5_like" | grep -c .)"

    if [ "$canonical_count" -eq 1 ] && [ "$j5_like_count" -eq 1 ]; then
        return 0
    fi

    if [ "$canonical_count" -eq 0 ] && [ "$j5_like_count" -eq 0 ]; then
        J5_CONTAINER_DIAGNOSTIC="canonical johnny-five container is not running"
    elif [ "$canonical_count" -eq 0 ]; then
        J5_CONTAINER_DIAGNOSTIC="a non-canonical Johnny-Five-like container is running ($(printf '%s' "$j5_like" | tr '\n' ' ')); refusing to attach"
    else
        J5_CONTAINER_DIAGNOSTIC="multiple Johnny-Five-like containers are running ($(printf '%s' "$j5_like" | tr '\n' ' ')); refusing to attach"
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
