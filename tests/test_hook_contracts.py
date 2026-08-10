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
    return subprocess.run(
        ["node", str(deployed_hooks / hook_name)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=env,
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


@pytest.mark.parametrize("hook_name", HOOK_FILES)
def test_runtime_hook_source_is_platform_neutral(hook_name: str) -> None:
    source = (HOOKS_DIR / hook_name).read_text(encoding="utf-8")

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
