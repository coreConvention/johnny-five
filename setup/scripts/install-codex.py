#!/usr/bin/env python3
"""Install Johnny-Five's versioned hooks into a Codex user profile."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


J5_HOOK_FILES = (
    "session-start-recall.sh",
    "memory-context-inject.sh",
    "user-prompt-correction.sh",
    "precompact-enforce.sh",
    "tool-failure-tracker.js",
    "memory-discipline-track.js",
    "memory-discipline-enforce.js",
)
J5_RUNTIME_FILES = ("j5-runtime.sh", "j5-runtime.js")
BEGIN_MARKER = "<!-- BEGIN johnny-five-codex -->"
END_MARKER = "<!-- END johnny-five-codex -->"
CANONICAL_WINDOWS_SOURCE = Path(r"Z:\Personal\johnny-five")


class InstallerError(RuntimeError):
    """Raised when installation cannot proceed without risking user state."""


class CodexInstaller:
    """Merge Johnny-Five-owned Codex artifacts while preserving other content."""

    error_type = InstallerError

    def __init__(self, home: Path, source_root: Path) -> None:
        self.home = home
        self.source_root = source_root
        self.codex_home = home / ".codex"
        self.hooks_dir = self.codex_home / "hooks"
        self.hooks_json = self.codex_home / "hooks.json"
        self.agents_md = self.codex_home / "AGENTS.md"
        self.source_hooks = source_root / "setup" / "hooks"
        self.codex_setup = source_root / "setup" / "codex"

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"hooks": {}}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise InstallerError(f"Refusing malformed JSON at {path}: {error}") from error
        if not isinstance(value, dict) or not isinstance(value.get("hooks", {}), dict):
            raise InstallerError(f"Refusing invalid hooks structure at {path}")
        value.setdefault("hooks", {})
        return value

    def _render_manifest(self) -> dict[str, Any]:
        template_path = self.codex_setup / "hooks.json.enforced.snippet"
        try:
            template = json.loads(template_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise InstallerError(f"Cannot load Codex hook template: {error}") from error

        replacements = {
            "{{CODEX_HOOKS_DIR_POSIX}}": self.hooks_dir.as_posix(),
            "{{CODEX_HOOKS_DIR_WINDOWS}}": str(self.hooks_dir),
        }

        def replace(value: Any) -> Any:
            if isinstance(value, str):
                for old, new in replacements.items():
                    value = value.replace(old, new)
                return value
            if isinstance(value, list):
                return [replace(item) for item in value]
            if isinstance(value, dict):
                return {key: replace(item) for key, item in value.items()}
            return value

        return replace(template)

    @staticmethod
    def _is_j5_handler(handler: dict[str, Any]) -> bool:
        if handler.get("_johnny_five") is True:
            return True
        command = " ".join(
            str(handler.get(key, "")) for key in ("command", "commandWindows")
        )
        return any(name in command for name in J5_HOOK_FILES)

    def _without_j5_handlers(self, manifest: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(manifest)
        hooks = result.setdefault("hooks", {})
        for event in list(hooks):
            retained_entries: list[dict[str, Any]] = []
            for entry in hooks[event]:
                retained = [
                    handler
                    for handler in entry.get("hooks", [])
                    if not self._is_j5_handler(handler)
                ]
                if retained:
                    updated = copy.deepcopy(entry)
                    updated["hooks"] = retained
                    retained_entries.append(updated)
            if retained_entries:
                hooks[event] = retained_entries
            else:
                del hooks[event]
        return result

    def _merged_manifest(self, current: dict[str, Any]) -> dict[str, Any]:
        result = self._without_j5_handlers(current)
        desired = self._render_manifest()
        hooks = result.setdefault("hooks", {})
        for event, entries in desired["hooks"].items():
            hooks.setdefault(event, []).extend(copy.deepcopy(entries))
        return result

    def _render_agents(self, current: str, *, remove: bool = False) -> str:
        start = current.find(BEGIN_MARKER)
        end = current.find(END_MARKER)
        if (start == -1) != (end == -1) or (start != -1 and end < start):
            raise InstallerError(f"Refusing malformed Johnny-Five marker block in {self.agents_md}")

        replacement = ""
        if not remove:
            replacement = (self.codex_setup / "AGENTS.md.snippet").read_text(encoding="utf-8").strip()

        if start != -1:
            end += len(END_MARKER)
            updated = current[:start] + replacement + current[end:]
        elif remove:
            updated = current
        else:
            separator = "" if not current else ("\n" if current.endswith("\n") else "\n\n")
            updated = current + separator + replacement + "\n"
        return updated

    @staticmethod
    def _serialized_json(value: dict[str, Any]) -> bytes:
        return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    @staticmethod
    def _backup_path(path: Path) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate = path.with_name(f"{path.name}.bak-{stamp}")
        suffix = 1
        while candidate.exists():
            candidate = path.with_name(f"{path.name}.bak-{stamp}-{suffix}")
            suffix += 1
        return candidate

    def _write_if_changed(
        self,
        path: Path,
        content: bytes,
        changes: list[str],
        *,
        dry_run: bool,
    ) -> None:
        if path.exists() and path.read_bytes() == content:
            return
        changes.append(f"write {path}")
        if dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, self._backup_path(path))
        path.write_bytes(content)

    def install(self, *, dry_run: bool = False) -> list[str]:
        current_manifest = self._read_json(self.hooks_json)
        current_agents = self.agents_md.read_text(encoding="utf-8") if self.agents_md.exists() else ""
        desired_manifest = self._merged_manifest(current_manifest)
        desired_agents = self._render_agents(current_agents)
        changes: list[str] = []

        for name in J5_HOOK_FILES:
            self._write_if_changed(
                self.hooks_dir / name,
                (self.source_hooks / name).read_bytes(),
                changes,
                dry_run=dry_run,
            )
        for name in J5_RUNTIME_FILES:
            self._write_if_changed(
                self.hooks_dir / "lib" / name,
                (self.source_hooks / "lib" / name).read_bytes(),
                changes,
                dry_run=dry_run,
            )
        self._write_if_changed(
            self.hooks_json,
            self._serialized_json(desired_manifest),
            changes,
            dry_run=dry_run,
        )
        self._write_if_changed(
            self.agents_md,
            desired_agents.encode("utf-8"),
            changes,
            dry_run=dry_run,
        )
        return changes

    def verify(self) -> list[str]:
        errors: list[str] = []
        for name in J5_HOOK_FILES:
            source = self.source_hooks / name
            target = self.hooks_dir / name
            if not target.exists() or target.read_bytes() != source.read_bytes():
                errors.append(f"hook drift: {target}")
        for name in J5_RUNTIME_FILES:
            source = self.source_hooks / "lib" / name
            target = self.hooks_dir / "lib" / name
            if not target.exists() or target.read_bytes() != source.read_bytes():
                errors.append(f"runtime drift: {target}")
        try:
            current = self._read_json(self.hooks_json)
            if self._merged_manifest(current) != current:
                errors.append(f"manifest drift: {self.hooks_json}")
        except InstallerError as error:
            errors.append(str(error))
        agents = self.agents_md.read_text(encoding="utf-8") if self.agents_md.exists() else ""
        try:
            if self._render_agents(agents) != agents:
                errors.append(f"AGENTS drift: {self.agents_md}")
        except InstallerError as error:
            errors.append(str(error))
        return errors

    def uninstall(self, *, dry_run: bool = False) -> list[str]:
        current_manifest = self._read_json(self.hooks_json)
        current_agents = self.agents_md.read_text(encoding="utf-8") if self.agents_md.exists() else ""
        desired_manifest = self._without_j5_handlers(current_manifest)
        desired_agents = self._render_agents(current_agents, remove=True)
        changes: list[str] = []

        self._write_if_changed(
            self.hooks_json,
            self._serialized_json(desired_manifest),
            changes,
            dry_run=dry_run,
        )
        self._write_if_changed(
            self.agents_md,
            desired_agents.encode("utf-8"),
            changes,
            dry_run=dry_run,
        )
        for path in [
            *(self.hooks_dir / name for name in J5_HOOK_FILES),
            *(self.hooks_dir / "lib" / name for name in J5_RUNTIME_FILES),
        ]:
            if not path.exists():
                continue
            changes.append(f"remove {path}")
            if not dry_run:
                shutil.copy2(path, self._backup_path(path))
                path.unlink()
        return changes


def _validate_canonical_source(source_root: Path) -> None:
    if os.name != "nt":
        return
    if str(source_root.resolve()).casefold() != str(CANONICAL_WINDOWS_SOURCE.resolve()).casefold():
        raise InstallerError(
            f"Run this installer from {CANONICAL_WINDOWS_SOURCE}; refusing source {source_root.resolve()}"
        )


def _validate_container_inventory() -> None:
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            text=True,
            capture_output=True,
            check=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallerError(f"Cannot inspect Docker container inventory: {error}") from error
    j5_like = [name for name in result.stdout.splitlines() if "johnny-five" in name.lower()]
    if len(j5_like) > 1:
        raise InstallerError(f"Multiple Johnny-Five-like containers exist: {', '.join(j5_like)}")
    if j5_like and j5_like != ["johnny-five"]:
        raise InstallerError(f"Non-canonical Johnny-Five container exists: {j5_like[0]}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--install", action="store_true")
    action.add_argument("--verify", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    source_root = Path(__file__).resolve().parents[2]
    try:
        _validate_canonical_source(source_root)
        _validate_container_inventory()
        installer = CodexInstaller(home=Path.home(), source_root=source_root)
        if args.verify:
            errors = installer.verify()
            for error in errors:
                print(error)
            return 1 if errors else 0
        if args.uninstall:
            changes = installer.uninstall()
        else:
            changes = installer.install(dry_run=args.dry_run)
        for change in changes:
            print(change)
        if not changes:
            print("Johnny-Five Codex integration is already current.")
        print("Project MCP configuration snippet:")
        print((source_root / "setup" / "codex" / "config.toml.snippet").read_text(encoding="utf-8"))
        return 0
    except InstallerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
