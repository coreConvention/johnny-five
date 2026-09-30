"""SessionStart hook renderer contract (issues #28/#29).

The Python embedded in ``setup/hooks/session-start-recall.sh`` is executed
in-process with recall stubbed and the resume lookup pointed at a seeded DB.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from claude_memory import config
from claude_memory.db import connection as connection_module
from claude_memory.db import queries
from claude_memory.mcp import tools

from tests.conftest import _TEST_SCHEMA_SQL

HOOK: Path = Path(__file__).parents[1] / "setup" / "hooks" / "session-start-recall.sh"
CWD: str = "Z:/proj/app"
HDR: str = "## Last session-state"


def _hook_source() -> str:
    lines: list[str] = HOOK.read_text(encoding="utf-8").splitlines()
    start: int = next(i for i, ln in enumerate(lines) if "<<'PYEOF'" in ln)
    end: int = next(i for i in range(start + 1, len(lines)) if lines[i] == "PYEOF")
    return "\n".join(lines[start + 1 : end])


def _rr(id: str, type: str, content: str, tags: list[str] | None = None) -> dict[str, Any]:
    return {"id": id, "type": type, "tags": tags or [], "content": content,
            "importance": 6.0, "created_at": "2026-09-01T00:00:00+00:00"}


class _Tracked:
    """Delegating connection wrapper that records close()."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.closed: int = 0

    def close(self) -> None:
        self.closed += 1
        self._conn.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class Harness:
    def __init__(self, mp: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.mp = mp
        self.db: Path = tmp_path / "hook.db"
        self.calls: list[dict[str, Any]] = []
        self.opened: list[_Tracked] = []
        self.results: list[dict[str, Any]] = [_rr("x", "lesson", "l")]
        c = sqlite3.connect(str(self.db))
        c.executescript(_TEST_SCHEMA_SQL)
        c.close()
        mp.setenv("MEMORY_DB_PATH", str(self.db))
        mp.delenv("NB_SESSION_ROOT", raising=False)
        config.get_settings.cache_clear()

        def _open(db_path: Path, embedding_dim: int = 384) -> sqlite3.Connection:
            # Real opener needs sqlite-vec (stubbed in tests); lookup only
            # reads the plain memories table.
            assert Path(db_path) == self.db
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            tracked = _Tracked(conn)
            self.opened.append(tracked)
            return tracked  # type: ignore[return-value]

        async def _recall(**kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {"results": self.results}

        mp.setattr(connection_module, "get_connection", _open)
        mp.setattr(tools, "tool_memory_recall", _recall)

    def seed(self, created_at: str, *, tags: list[str] | None = None,
             content: str = "resume body", id: str = "r1") -> None:
        c = sqlite3.connect(str(self.db))
        c.execute(
            "INSERT INTO memories (id, content, type, tags, created_at, updated_at,"
            " last_accessed, importance, project_dir) VALUES (?, ?, 'project', ?, ?, ?, ?, 7.5, ?)",
            (id, content, json.dumps(tags or ["session-state"]), created_at,
             created_at, created_at, CWD),
        )
        c.commit()
        c.close()

    def run(self, capsys: Any, cwd: str = CWD, root: str | None = None) -> str:
        self.mp.setenv("NB_CWD", cwd)
        if root:
            self.mp.setenv("NB_SESSION_ROOT", root)
        else:
            self.mp.delenv("NB_SESSION_ROOT", raising=False)
        capsys.readouterr()
        exec(compile(_hook_source(), "session-start-recall", "exec"), {"__name__": "__main__"})
        return json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]


@pytest.fixture()
def hook(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    yield Harness(monkeypatch, tmp_path)
    config.get_settings.cache_clear()


@pytest.mark.parametrize(("cwd", "expected"), [
    ("Z:/proj/app", "session start resume context app"),
    ("/home/u/proj", "session start resume context proj"),
])
def test_initial_context_names_the_project(hook: Harness, capsys, cwd: str, expected: str) -> None:
    """Pre-change: initial_context was the constant 'session start resume context'."""
    hook.run(capsys, cwd)
    assert hook.calls[0]["initial_context"] == expected


def test_found_renders_utc_label_and_fence(hook: Harness, capsys) -> None:
    """Pre-change: section came from ranked recall with an unconverted label."""
    hook.seed("2026-09-29T22:30:00+02:00")
    text: str = hook.run(capsys)
    assert f"{HDR}  (2026-09-29 20:30 UTC, importance 7.5)\n```\nresume body\n```" in text
    assert "mechanical floor" not in text


def test_mechanical_floor_note_iff_tagged(hook: Harness, capsys) -> None:
    hook.seed("2026-09-29T20:30:00+00:00", tags=["session-state", "mechanical-floor"])
    assert "mechanical floor" in hook.run(capsys)


def test_calendar_edge_label_falls_back_to_raw_string(hook: Harness, capsys) -> None:
    """UTC conversion overflows at year 1: raw value, no 'UTC' suffix."""
    hook.seed("0001-01-01T00:00:00+00:01")
    assert f"{HDR}  (0001-01-01T00:00:00+00:01, importance 7.5)" in hook.run(capsys)


def test_none_state(hook: Harness, capsys) -> None:
    text: str = hook.run(capsys)
    assert f"{HDR}\nNo session-state memory is recorded for this project_dir yet." in text


def test_unavailable_state_and_no_pick_from_recall(hook: Harness, capsys) -> None:
    hook.mp.delattr(queries, "get_latest_session_state", raising=False)
    hook.results = [_rr("s", "project", "STATEBODY", ["session-state"])]
    text: str = hook.run(capsys)
    assert (f"{HDR}\nUnavailable: the running johnny-five server predates "
            "get_latest_session_state. Rebuild the image and recreate the container.") in text
    assert f"{HDR}  (" not in text


def test_error_state(hook: Harness, capsys) -> None:
    def boom(conn: Any, project_dir: Any) -> Any:
        raise RuntimeError("disk on fire")

    hook.mp.setattr(queries, "get_latest_session_state", boom, raising=False)
    text: str = hook.run(capsys)
    assert (f"{HDR}\nLookup failed (disk on fire). "
            "Call memory_search with tags=['session-state'] manually.") in text


def test_suppression_is_by_id_only(hook: Harness, capsys) -> None:
    """Pins current behaviour (it also passes on the old hook): only the rendered
    row is suppressed; a different session-state-tagged lesson still shows."""
    hook.seed("2026-09-29T20:30:00+00:00", content="RESUMEBODY")
    hook.results = [_rr("r1", "project", "RESUMEBODY", ["session-state"]),
                    _rr("l2", "lesson", "LESSONSS", ["kind:session-state"])]
    text: str = hook.run(capsys)
    assert text.count("RESUMEBODY") == 1
    assert "LESSONSS" in text.split("## Top lessons for this project", 1)[1]


def test_empty_recall_message_unchanged(hook: Harness, capsys) -> None:
    hook.results = []
    assert hook.run(capsys) == (
        "session-start-recall: johnny-five reachable but no memories for "
        f"project_dir={CWD!r} yet. Store insights as you learn them.")


def test_blank_cwd_emits_single_json_with_none_state(hook: Harness, capsys) -> None:
    """A blank project_dir skips the lookup (None) and still emits one JSON object."""
    text: str = hook.run(capsys, "")
    assert hook.calls[0]["initial_context"] == "session start resume context"
    assert f"{HDR}\nNo session-state memory is recorded for this project_dir yet." in text


def test_worktree_note_iff_session_root(hook: Harness, capsys) -> None:
    hook.seed("2026-09-29T20:30:00+00:00")
    note: str = "every checkout of this project shares one session-state"
    assert note in hook.run(capsys, root="/wt/checkout")
    assert note not in hook.run(capsys)


def test_connection_closed_after_successful_lookup(hook: Harness, capsys) -> None:
    hook.seed("2026-09-29T20:30:00+00:00")
    hook.run(capsys)
    assert [c.closed for c in hook.opened] == [1]


def test_connection_closed_after_raising_lookup(hook: Harness, capsys) -> None:
    def boom(conn: Any, project_dir: Any) -> Any:
        raise RuntimeError("disk on fire")

    hook.mp.setattr(queries, "get_latest_session_state", boom, raising=False)
    hook.run(capsys)
    assert [c.closed for c in hook.opened] == [1]


def test_invalid_iso_created_at_renders_raw_string(hook: Harness, capsys) -> None:
    """ValueError path (distinct from the OverflowError calendar-edge case)."""
    hook.seed("not-a-date")
    assert f"{HDR}  (not-a-date, importance 7.5)" in hook.run(capsys)


def test_worktree_note_absent_in_none_state(hook: Harness, capsys) -> None:
    """The note belongs to a rendered resume block, not to the none state."""
    text: str = hook.run(capsys, root="/wt/checkout")
    assert "No session-state memory is recorded" in text
    assert "every checkout of this project shares one session-state" not in text


def test_same_content_different_id_row_still_shown(hook: Harness, capsys) -> None:
    """Suppression keys on id: identical content under another id is a different memory."""
    hook.seed("2026-09-29T20:30:00+00:00", content="SAMEBODY")
    hook.results = [_rr("other", "lesson", "SAMEBODY")]
    text: str = hook.run(capsys)
    assert "SAMEBODY" in text.split("## Top lessons for this project", 1)[1]


def test_same_id_different_content_row_is_suppressed(hook: Harness, capsys) -> None:
    hook.seed("2026-09-29T20:30:00+00:00", content="RESUMEBODY")
    hook.results = [_rr("r1", "project", "STALEVERSION")]
    text: str = hook.run(capsys)
    assert "RESUMEBODY" in text
    assert "STALEVERSION" not in text


@pytest.mark.parametrize("row_type", ["project", "lesson", "user", "feedback"])
def test_same_id_row_suppressed_for_every_type(hook: Harness, capsys, row_type: str) -> None:
    """The guard is id-only: no recalled row type may repeat the resume row."""
    hook.seed("2026-09-29T20:30:00+00:00", content="RESUMEBODY")
    hook.results = [_rr("r1", row_type, "RESUMEBODY")]
    assert hook.run(capsys).count("RESUMEBODY") == 1


@pytest.mark.parametrize("cwd", [
    "Z:" + chr(92) + "proj" + chr(92) + "app",
    "Z:/proj/app/",
    "Z:" + chr(92) + "proj" + chr(92) + "app" + chr(92),
], ids=["backslash", "trailing-slash", "trailing-backslash"])
def test_initial_context_basename_for_windows_forms(hook: Harness, capsys, cwd: str) -> None:
    hook.run(capsys, cwd)
    assert hook.calls[0]["initial_context"] == "session start resume context app"


def test_naive_created_at_label_is_utc(hook: Harness, capsys) -> None:
    """A naive timestamp is UTC, not the raw-string fallback."""
    hook.seed("2026-09-29T21:30:00")
    assert f"{HDR}  (2026-09-29 21:30 UTC, importance 7.5)" in hook.run(capsys)
