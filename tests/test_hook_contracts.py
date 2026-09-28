"""Contract tests for the cross-runtime Johnny-Five hook integration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "setup" / "hooks"
CODEX_DIR = ROOT / "setup" / "codex"
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "hooks"

HOOK_FILES = (
    "session-start-recall.sh",
    "memory-context-inject.sh",
    "user-prompt-correction.sh",
    "precompact-enforce.sh",
    "tool-failure-tracker.js",
    "memory-discipline-track.js",
    "memory-discipline-enforce.js",
)

RUNTIME_FILES = HOOK_FILES + (
    "lib/j5-runtime.sh",
    "lib/j5-runtime.js",
)

FIXTURE_REQUIRED_FIELDS: dict[str, set[str]] = {
    "codex-session-start.json": {"session_id", "cwd", "hook_event_name", "source"},
    "codex-user-prompt-submit.json": {
        "session_id",
        "cwd",
        "hook_event_name",
        "prompt",
    },
    "codex-pre-tool-use.json": {
        "session_id",
        "cwd",
        "hook_event_name",
        "tool_name",
        "tool_input",
    },
    "codex-post-tool-use.json": {
        "session_id",
        "cwd",
        "hook_event_name",
        "tool_name",
        "tool_input",
        "tool_response",
    },
    "codex-pre-compact.json": {"session_id", "cwd", "hook_event_name", "trigger"},
    "codex-stop.json": {
        "session_id",
        "cwd",
        "hook_event_name",
        "stop_hook_active",
    },
}

FORBIDDEN_RUNTIME_TEXT = (
    "CLAUDE_PROJECT_DIR",
    "~/.claude/hooks/state",
    "johnny-five-johnny-five-1",
    "MultiEdit",
    "NotebookEdit",
)

DOCUMENTATION_FILES = (
    ROOT / "README.md",
    ROOT / "docs" / "INTEGRATION.md",
    ROOT / "docs" / "AGENT_NOTES.md",
    ROOT / "docs" / "BACKUP_AND_RESTORE.md",
    ROOT / "docs" / "BEST_PRACTICES.md",
    ROOT / "docs" / "CLAUDE_MD_SNIPPETS.md",
    ROOT / "docs" / "AGENTS_MD_SNIPPETS.md",
    ROOT / "setup" / "CLAUDE.md.snippet",
    ROOT / "setup" / "codex" / "AGENTS.md.snippet",
    ROOT / ".claude" / "commands" / "integrate.md",
)

FORBIDDEN_DOCUMENTATION_TEXT = (
    "johnny-five-johnny-five-1",
    "docker attach",
    "--transport stdio",
    "CLAUDE_PROJECT_DIR",
)


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _run_node_hook(
    hook_name: str,
    payload: dict[str, Any],
    temp_home: Path,
) -> subprocess.CompletedProcess[str]:
    deployed_hooks = temp_home / "hooks"
    deployed_hooks.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HOOKS_DIR / hook_name, deployed_hooks / hook_name)
    runtime_lib = HOOKS_DIR / "lib"
    if runtime_lib.exists():
        shutil.copytree(runtime_lib, deployed_hooks / "lib", dirs_exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(temp_home)
    env["USERPROFILE"] = str(temp_home)
    node = shutil.which("node")
    if node is None and os.name == "nt":
        node = str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe")
    assert node is not None
    return subprocess.run(
        [node, str(deployed_hooks / hook_name)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=10,
    )


def _git_bash_path(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    tail = resolved.as_posix()[len(resolved.drive) :].lstrip("/")
    return f"/{drive}/{tail}"


def _bash_executable() -> str:
    # On Windows, PATH's bash may be WSL's, which cannot run these hooks.
    bash = shutil.which("bash")
    if os.name == "nt":
        git_bash = (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Git"
            / "bin"
            / "bash.exe"
        )
        if git_bash.is_file():
            bash = str(git_bash)
    assert bash is not None
    return bash


def _run_correction_hook(
    payload: dict[str, Any],
    temp_home: Path,
) -> subprocess.CompletedProcess[str]:
    deployed_hooks = temp_home / "hooks"
    deployed_hooks.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HOOKS_DIR / "user-prompt-correction.sh", deployed_hooks)
    shutil.copytree(HOOKS_DIR / "lib", deployed_hooks / "lib", dirs_exist_ok=True)

    bash = _bash_executable()

    hook_path = _git_bash_path(deployed_hooks / "user-prompt-correction.sh")
    wrapper = r'''
docker() {
    if [ "$1" = "ps" ]; then
        printf 'johnny-five\n'
        return 0
    fi
    if [ "$1" = "exec" ]; then
        cat >/dev/null
        printf '%s' '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"synthetic correction search"}}'
        return 0
    fi
    return 1
}
export -f docker
"$1"
'''
    return subprocess.run(
        [bash, "-c", wrapper, "j5-test", hook_path],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize("fixture_name", FIXTURE_REQUIRED_FIELDS)
def test_codex_fixture_uses_official_common_fields(fixture_name: str) -> None:
    payload = _fixture(fixture_name)

    assert FIXTURE_REQUIRED_FIELDS[fixture_name] <= payload.keys()
    assert payload["cwd"] == r"Z:\Personal\w31rd.com"
    assert payload["session_id"]


def test_pre_tool_fixture_uses_codex_apply_patch_shape() -> None:
    payload = _fixture("codex-pre-tool-use.json")

    assert payload["tool_name"] == "apply_patch"
    assert isinstance(payload["tool_input"]["command"], str)


def test_stop_fixture_exercises_first_stop_attempt() -> None:
    payload = _fixture("codex-stop.json")

    assert payload["stop_hook_active"] is False


@pytest.mark.parametrize("runtime_file", RUNTIME_FILES)
def test_runtime_hook_source_is_platform_neutral(runtime_file: str) -> None:
    source = (HOOKS_DIR / runtime_file).read_text(encoding="utf-8")

    found = [value for value in FORBIDDEN_RUNTIME_TEXT if value in source]
    assert found == []


def test_codex_manifest_is_command_only_and_uses_supported_matchers() -> None:
    manifest_path = CODEX_DIR / "hooks.json.enforced.snippet"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    serialized = json.dumps(manifest)

    assert '"type": "prompt"' not in serialized
    hooks = manifest["hooks"]
    assert hooks["SessionStart"][0]["matcher"] == "startup|resume|clear|compact"
    assert hooks["PreToolUse"][0]["matcher"] == "Bash|apply_patch"
    assert hooks["PreCompact"][0]["matcher"] == "manual|auto"
    assert "matcher" not in hooks["PostToolUse"][0]
    assert "matcher" not in hooks["UserPromptSubmit"][0]
    assert "matcher" not in hooks["Stop"][0]

    for event_entries in hooks.values():
        for entry in event_entries:
            for handler in entry["hooks"]:
                assert handler["type"] == "command"
                commands = [handler.get("command", ""), handler.get("commandWindows", "")]
                command_text = " ".join(commands)
                match = re.search(
                    r"(session-start-recall\.sh|memory-context-inject\.sh|"
                    r"user-prompt-correction\.sh|precompact-enforce\.sh|"
                    r"tool-failure-tracker\.js|memory-discipline-track\.js|"
                    r"memory-discipline-enforce\.js)",
                    command_text,
                )
                assert match is not None
                assert (HOOKS_DIR / match.group(1)).is_file()


def test_codex_config_snippet_preserves_proven_transport() -> None:
    snippet = (CODEX_DIR / "config.toml.snippet").read_text(encoding="utf-8")

    assert "command = 'C:\\Program Files\\nodejs\\node.exe'" in snippet
    assert "supergateway@3.4.3" in snippet
    assert "http://127.0.0.1:8787/sse/" in snippet
    assert "enabled = true" in snippet
    assert "required = true" in snippet


@pytest.mark.parametrize("document", DOCUMENTATION_FILES, ids=lambda path: path.name)
def test_integration_documentation_rejects_unsafe_or_stale_guidance(document: Path) -> None:
    text = document.read_text(encoding="utf-8")

    found = [value for value in FORBIDDEN_DOCUMENTATION_TEXT if value in text]
    assert found == []
    for line in text.splitlines():
        assert not ("Codex" in line and ".mcp.json" in line)


@pytest.mark.parametrize("document", DOCUMENTATION_FILES, ids=lambda path: path.name)
def test_integration_documentation_local_links_resolve(document: Path) -> None:
    text = document.read_text(encoding="utf-8")
    links = re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text)

    for link in links:
        target = link.split("#", 1)[0].strip().strip("<>")
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        assert (document.parent / target).resolve().exists(), f"broken link in {document}: {link}"


def test_stop_hook_emits_valid_block_json(tmp_path: Path) -> None:
    payload = _fixture("codex-stop.json")
    state_dir = tmp_path / "hooks" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "memory-discipline-codex-session-001.json").write_text(
        json.dumps({"edits": 3, "stores": 0, "searches": 0}),
        encoding="utf-8",
    )

    result = _run_node_hook("memory-discipline-enforce.js", payload, tmp_path)

    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["decision"] == "block"
    assert output["reason"]


def test_stop_hook_active_does_not_create_continuation_loop(tmp_path: Path) -> None:
    payload = _fixture("codex-stop.json") | {"stop_hook_active": True}

    result = _run_node_hook("memory-discipline-enforce.js", payload, tmp_path)

    assert result.returncode == 0
    assert result.stdout == ""


def test_correction_hook_matches_codex_correction_prompt(tmp_path: Path) -> None:
    payload = _fixture("codex-user-prompt-submit.json") | {
        "prompt": "Actually, use the canonical Johnny-Five container."
    }

    result = _run_correction_hook(payload, tmp_path)

    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"] == {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "synthetic correction search",
    }
    state = json.loads(
        (
            tmp_path
            / "hooks"
            / "state"
            / "memory-discipline-codex-session-001.json"
        ).read_text(encoding="utf-8")
    )
    assert state["correction_seen"] is True


# ---------------------------------------------------------------------------
# Project scope resolution (issue #38). The server matches project_dir by exact
# path, so every checkout of a repository must resolve to its main checkout.
# ---------------------------------------------------------------------------


_SCOPE_OVERRIDES = ("J5_PROJECT_DIR", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The test process environment minus anything that would override scope."""
    env = {key: value for key, value in os.environ.items() if key not in _SCOPE_OVERRIDES}
    return env | (extra or {})


def _git(repo: Path, *args: str, env: dict[str, str]) -> None:
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


@pytest.fixture()
def repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A main checkout plus a linked worktree on branch ``topic``."""
    # Isolate the fixture from the machine's git config (signing, hooksPath).
    empty_config = tmp_path / "gitconfig"
    empty_config.write_text("", encoding="utf-8")
    env = _clean_env({"GIT_CONFIG_GLOBAL": str(empty_config), "GIT_CONFIG_NOSYSTEM": "1"})

    main = tmp_path / "main"
    (main / "sub").mkdir(parents=True)
    _git(main, "init", "-q", "-b", "main", env=env)
    _git(
        main,
        "-c",
        "user.name=j5-test",
        "-c",
        "user.email=j5-test@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
        env=env,
    )
    worktree = tmp_path / "worktrees" / "brave-turing-1a2b3c"
    _git(main, "worktree", "add", "-q", "-b", "topic", str(worktree), env=env)
    return main, worktree


def _is_dir(reported: str, expected: Path) -> bool:
    return bool(reported) and Path(reported).resolve() == expected.resolve()


def _run_runtime(
    script: str,
    *args: str,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Source the shared runtime and run a snippet against it."""
    lib = _git_bash_path(HOOKS_DIR / "lib" / "j5-runtime.sh")
    return subprocess.run(
        [_bash_executable(), "-c", f'source "{lib}"\n{script}', "j5-test", *args],
        cwd=cwd,
        env=_clean_env(env),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def _resolve_project(
    payload_cwd: str,
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> str:
    result = _run_runtime('j5_project_cwd "$1"', payload_cwd, cwd=cwd, env=env)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_project_dir_of_main_checkout_is_unchanged(repo_with_worktree: tuple[Path, Path]) -> None:
    main, _ = repo_with_worktree

    assert _is_dir(_resolve_project(str(main), cwd=main), main)


def test_project_dir_of_worktree_is_its_main_checkout(repo_with_worktree: tuple[Path, Path]) -> None:
    main, worktree = repo_with_worktree

    assert _is_dir(_resolve_project(str(worktree), cwd=worktree), main)


def test_project_dir_of_subdirectory_is_its_main_checkout(repo_with_worktree: tuple[Path, Path]) -> None:
    main, _ = repo_with_worktree

    assert _is_dir(_resolve_project(str(main / "sub"), cwd=main), main)


def test_project_dir_override_wins(repo_with_worktree: tuple[Path, Path]) -> None:
    _, worktree = repo_with_worktree

    reported = _resolve_project(
        str(worktree), cwd=worktree, env={"J5_PROJECT_DIR": "Z:/Personal/override"}
    )

    assert reported == "Z:/Personal/override"


def test_project_dir_outside_git_falls_back_to_the_directory(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    # The ceiling stops git from finding a repository above the temp dir.
    reported = _resolve_project(
        str(plain), cwd=plain, env={"GIT_CEILING_DIRECTORIES": str(tmp_path)}
    )

    assert reported == str(plain)


def test_missing_payload_cwd_falls_back_to_the_hook_directory(
    repo_with_worktree: tuple[Path, Path],
) -> None:
    main, worktree = repo_with_worktree

    for payload_cwd in ("", str(worktree / "deleted-since")):
        assert _is_dir(_resolve_project(payload_cwd, cwd=worktree), main)


def test_project_dir_ignores_an_inherited_git_dir(
    repo_with_worktree: tuple[Path, Path], tmp_path: Path
) -> None:
    main, worktree = repo_with_worktree
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], env=_clean_env(), check=True, capture_output=True)

    reported = _resolve_project(
        str(worktree), cwd=worktree, env={"GIT_DIR": str(other / ".git")}
    )

    assert _is_dir(reported, main)


# ---------------------------------------------------------------------------
# Hooks end to end with the Codex payload shape. A stub `docker` reports the
# running containers and echoes the scope the hook would send to Johnny-Five.
# ---------------------------------------------------------------------------

_SCOPE_KEYS = ("NB_CWD", "NB_SESSION_CWD", "NB_SESSION_ROOT", "NB_BRANCH", "NB_SID")

_DOCKER_SCOPE_STUB = r'''
docker() {
    if [ "$1" = "ps" ]; then
        printf '%s\n' $J5_TEST_CONTAINERS
        return 0
    fi
    if [ "$1" = "exec" ]; then
        cat >/dev/null
        python -c '
import json, os, sys
scope = {key: os.environ.get(key, "") for key in sys.argv[2:]}
print(json.dumps({"hookSpecificOutput": {"hookEventName": sys.argv[1], "additionalContext": json.dumps(scope)}}), end="")
' "$J5_TEST_EVENT" NB_CWD NB_SESSION_CWD NB_SESSION_ROOT NB_BRANCH NB_SID
        return 0
    fi
    return 1
}
export -f docker
"$1"
'''


def _run_hook_for_scope(
    hook_name: str,
    fixture_name: str,
    session_cwd: Path,
    tmp_path: Path,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    deployed_hooks = tmp_path / "home" / "hooks"
    deployed_hooks.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HOOKS_DIR / hook_name, deployed_hooks)
    shutil.copytree(HOOKS_DIR / "lib", deployed_hooks / "lib", dirs_exist_ok=True)
    payload = _fixture(fixture_name) | {"cwd": str(session_cwd)}

    result = subprocess.run(
        [_bash_executable(), "-c", _DOCKER_SCOPE_STUB, "j5-test", _git_bash_path(deployed_hooks / hook_name)],
        input=json.dumps(payload),
        cwd=session_cwd,
        env=_clean_env(
            {
                "J5_TEST_CONTAINERS": "johnny-five johnny-five-dashboard",
                "J5_TEST_EVENT": payload["hook_event_name"],
            }
            | (env or {})
        ),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    scope: dict[str, str] = json.loads(output["hookSpecificOutput"]["additionalContext"])
    assert set(scope) == set(_SCOPE_KEYS)
    return scope


def test_session_start_in_worktree_recalls_main_checkout_scope(
    repo_with_worktree: tuple[Path, Path], tmp_path: Path
) -> None:
    main, worktree = repo_with_worktree

    scope = _run_hook_for_scope(
        "session-start-recall.sh", "codex-session-start.json", worktree, tmp_path
    )

    assert _is_dir(scope["NB_CWD"], main)
    assert _is_dir(scope["NB_SESSION_ROOT"], worktree)


def test_session_start_in_main_checkout_names_no_other_checkout(
    repo_with_worktree: tuple[Path, Path], tmp_path: Path
) -> None:
    main, _ = repo_with_worktree

    scope = _run_hook_for_scope(
        "session-start-recall.sh", "codex-session-start.json", main, tmp_path
    )

    assert _is_dir(scope["NB_CWD"], main)
    assert scope["NB_SESSION_ROOT"] == ""


def test_session_start_does_not_call_an_overridden_clone_a_worktree(
    repo_with_worktree: tuple[Path, Path], tmp_path: Path
) -> None:
    # A separate clone is its own main checkout; the override points it at a
    # shared scope, but that does not make it a worktree.
    main, _ = repo_with_worktree

    scope = _run_hook_for_scope(
        "session-start-recall.sh",
        "codex-session-start.json",
        main,
        tmp_path,
        env={"J5_PROJECT_DIR": "Z:/Personal/override"},
    )

    assert scope["NB_CWD"] == "Z:/Personal/override"
    assert scope["NB_SESSION_ROOT"] == ""


def test_precompact_scopes_to_main_checkout_but_records_the_worktree(
    repo_with_worktree: tuple[Path, Path], tmp_path: Path
) -> None:
    main, worktree = repo_with_worktree

    scope = _run_hook_for_scope(
        "precompact-enforce.sh", "codex-pre-compact.json", worktree, tmp_path
    )

    assert _is_dir(scope["NB_CWD"], main)
    assert _is_dir(scope["NB_SESSION_CWD"], worktree)
    assert scope["NB_BRANCH"] == "topic"
    assert scope["NB_SID"] == _fixture("codex-pre-compact.json")["session_id"]


def test_hooks_honor_the_project_override(
    repo_with_worktree: tuple[Path, Path], tmp_path: Path
) -> None:
    _, worktree = repo_with_worktree

    scope = _run_hook_for_scope(
        "precompact-enforce.sh",
        "codex-pre-compact.json",
        worktree,
        tmp_path,
        env={"J5_PROJECT_DIR": "Z:/Personal/override"},
    )

    assert scope["NB_CWD"] == "Z:/Personal/override"
    assert scope["NB_BRANCH"] == "topic"


# ---------------------------------------------------------------------------
# Canonical-container check (issue #35). A second memory server is the hazard;
# the dashboard sibling from docker-compose.yml is not.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("containers", "expected"),
    [
        ("johnny-five", "ATTACH"),
        ("johnny-five johnny-five-dashboard", "ATTACH"),
        ("redis johnny-five ravendb", "ATTACH"),
        (
            "johnny-five johnny-five-johnny-five-1",
            "REFUSE:multiple Johnny-Five-like containers are running "
            "(johnny-five johnny-five-johnny-five-1); refusing to attach",
        ),
        (
            "johnny-five johnny-five-dashboard johnny_five_orphan",
            "REFUSE:multiple Johnny-Five-like containers are running "
            "(johnny-five johnny_five_orphan); refusing to attach",
        ),
        (
            "johnny-five-johnny-five-1",
            "REFUSE:a non-canonical Johnny-Five-like container is running "
            "(johnny-five-johnny-five-1); refusing to attach",
        ),
        ("johnny-five-dashboard", "REFUSE:canonical johnny-five container is not running"),
        ("", "REFUSE:canonical johnny-five container is not running"),
    ],
)
def test_canonical_container_check(containers: str, expected: str, tmp_path: Path) -> None:
    script = (
        "docker() { printf '%s\\n' $J5_TEST_CONTAINERS; }\n"
        "if j5_require_canonical_container; then printf ATTACH; "
        'else printf "REFUSE:%s" "$J5_CONTAINER_DIAGNOSTIC"; fi'
    )

    result = _run_runtime(script, cwd=tmp_path, env={"J5_TEST_CONTAINERS": containers})

    assert result.stdout == expected


def test_known_siblings_match_the_compose_services() -> None:
    from tests.test_install_codex import _load_installer_module

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    services = set(re.findall(r"^\s*container_name:\s*(\S+)\s*$", compose, re.MULTILINE))
    runtime = (HOOKS_DIR / "lib" / "j5-runtime.sh").read_text(encoding="utf-8")
    runtime_match = re.search(r'^J5_KNOWN_SIBLINGS="([^"]*)"$', runtime, re.MULTILINE)
    assert runtime_match is not None
    runtime_siblings = set(runtime_match.group(1).split("|"))
    installer_siblings = set(_load_installer_module().J5_KNOWN_SIBLINGS)

    assert "johnny-five" in services
    assert services - {"johnny-five"} == runtime_siblings == installer_siblings
