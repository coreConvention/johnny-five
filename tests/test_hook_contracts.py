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


def _run_correction_hook(
    payload: dict[str, Any],
    temp_home: Path,
) -> subprocess.CompletedProcess[str]:
    deployed_hooks = temp_home / "hooks"
    deployed_hooks.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HOOKS_DIR / "user-prompt-correction.sh", deployed_hooks)
    shutil.copytree(HOOKS_DIR / "lib", deployed_hooks / "lib", dirs_exist_ok=True)

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
