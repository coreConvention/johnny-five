"""Behavioral tests for the Codex Johnny-Five installer."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "setup" / "scripts" / "install-codex.py"
HOOK_NAMES = {
    "session-start-recall.sh",
    "memory-context-inject.sh",
    "user-prompt-correction.sh",
    "precompact-enforce.sh",
    "tool-failure-tracker.js",
    "memory-discipline-track.js",
    "memory-discipline-enforce.js",
}


def _load_installer_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("install_codex", INSTALLER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _installer(home: Path) -> Any:
    module = _load_installer_module()
    return module.CodexInstaller(home=home, source_root=ROOT)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for candidate in sorted(path for path in root.rglob("*") if path.is_file()):
        if ".bak-" in candidate.name:
            continue
        digest.update(candidate.relative_to(root).as_posix().encode())
        digest.update(candidate.read_bytes())
    return digest.hexdigest()


def _handler_commands(manifest: dict[str, Any]) -> list[str]:
    commands: list[str] = []
    for entries in manifest.get("hooks", {}).values():
        for entry in entries:
            for handler in entry.get("hooks", []):
                commands.extend(
                    value
                    for value in (handler.get("command"), handler.get("commandWindows"))
                    if value
                )
    return commands


def test_clean_install_and_verify(tmp_path: Path) -> None:
    installer = _installer(tmp_path)

    changes = installer.install()

    codex_home = tmp_path / ".codex"
    assert changes
    assert {path.name for path in (codex_home / "hooks").iterdir() if path.is_file()} >= HOOK_NAMES
    assert (codex_home / "hooks" / "lib" / "j5-runtime.sh").is_file()
    assert (codex_home / "hooks" / "lib" / "j5-runtime.js").is_file()
    manifest = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    commands = _handler_commands(manifest)
    assert all(any(name in command for command in commands) for name in HOOK_NAMES)
    assert all("{{CODEX_HOOKS_DIR" not in command for command in commands)
    windows_commands = [
        handler["commandWindows"]
        for entries in manifest["hooks"].values()
        for entry in entries
        for handler in entry["hooks"]
        if handler.get("_johnny_five") is True
    ]
    assert all(command.startswith('"C:\\Program Files\\') for command in windows_commands)
    assert "<!-- BEGIN johnny-five-codex -->" in (codex_home / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    assert installer.verify() == []


def test_install_preserves_unrelated_hooks_and_is_idempotent(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    unrelated = {
        "matcher": "Task",
        "hooks": [
            {
                "type": "command",
                "command": "node C:/tools/worktree-rename-hint.js",
                "timeout": 7,
                "_custom": {"preserve": "exactly"},
            }
        ],
    }
    (codex_home / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [unrelated]}}, indent=2) + "\n",
        encoding="utf-8",
    )
    installer = _installer(tmp_path)

    installer.install()
    first_digest = _tree_digest(codex_home)
    first_manifest = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    installer.install()
    second_digest = _tree_digest(codex_home)
    second_manifest = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))

    assert unrelated in first_manifest["hooks"]["PreToolUse"]
    assert unrelated in second_manifest["hooks"]["PreToolUse"]
    assert first_digest == second_digest


def test_install_upgrades_changed_hook_and_creates_backup(tmp_path: Path) -> None:
    installer = _installer(tmp_path)
    installer.install()
    codex_home = tmp_path / ".codex"
    target = codex_home / "hooks" / "session-start-recall.sh"
    target.write_text("drifted", encoding="utf-8")
    hooks_json = codex_home / "hooks.json"
    manifest = json.loads(hooks_json.read_text(encoding="utf-8"))
    manifest["unrelated"] = "keep"
    hooks_json.write_text(json.dumps(manifest, indent=4), encoding="utf-8")

    installer.install()

    assert target.read_bytes() == (ROOT / "setup" / "hooks" / target.name).read_bytes()
    assert json.loads(hooks_json.read_text(encoding="utf-8"))["unrelated"] == "keep"
    assert list(codex_home.glob("hooks.json.bak-*"))


def test_malformed_hooks_json_is_refused_without_mutation(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    hooks_json = codex_home / "hooks.json"
    hooks_json.write_text("{not-json", encoding="utf-8")
    installer = _installer(tmp_path)

    with pytest.raises(installer.error_type):
        installer.install()

    assert hooks_json.read_text(encoding="utf-8") == "{not-json"
    assert not (codex_home / "hooks").exists()


def test_dry_run_reports_changes_without_writing(tmp_path: Path) -> None:
    installer = _installer(tmp_path)

    changes = installer.install(dry_run=True)

    assert changes
    assert not (tmp_path / ".codex").exists()


def test_uninstall_removes_only_j5_owned_content(tmp_path: Path) -> None:
    installer = _installer(tmp_path)
    installer.install()
    codex_home = tmp_path / ".codex"
    hooks_json = codex_home / "hooks.json"
    manifest = json.loads(hooks_json.read_text(encoding="utf-8"))
    unrelated = {
        "matcher": "Task",
        "hooks": [{"type": "command", "command": "node C:/tools/keep-me.js"}],
    }
    manifest["hooks"].setdefault("PreToolUse", []).insert(0, unrelated)
    hooks_json.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    agents = codex_home / "AGENTS.md"
    agents.write_text("User text before\n" + agents.read_text(encoding="utf-8") + "User text after\n", encoding="utf-8")

    installer.uninstall()

    result = json.loads(hooks_json.read_text(encoding="utf-8"))
    assert unrelated in result["hooks"]["PreToolUse"]
    assert not any(any(name in command for name in HOOK_NAMES) for command in _handler_commands(result))
    assert all(not (codex_home / "hooks" / name).exists() for name in HOOK_NAMES)
    agents_text = agents.read_text(encoding="utf-8")
    assert "User text before" in agents_text
    assert "User text after" in agents_text
    assert "<!-- BEGIN johnny-five-codex -->" not in agents_text
