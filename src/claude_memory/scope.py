"""Project-scope comparison helpers.

Project ownership is stored as a path, so comparisons must respect the path
rules of the originating platform rather than the platform running the server.
"""

from __future__ import annotations

import ntpath
import posixpath
import re


_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def _is_windows_project_dir(project_dir: str) -> bool:
    """Recognize absolute Windows drive and UNC paths on any host OS."""
    return bool(_WINDOWS_ABSOLUTE_PATH.match(project_dir)) or project_dir.startswith(
        ("\\\\", "//")
    )


def project_dir_basename(project_dir: str) -> str:
    """Return a project path's final component using its originating OS rules."""
    is_windows_path: bool = _is_windows_project_dir(project_dir)
    value: str = project_dir.rstrip("/\\")
    path_module = ntpath if is_windows_path else posixpath
    return path_module.basename(value)


def canonicalize_project_dir(project_dir: str | None) -> str | None:
    """Return a comparison-safe project scope without changing stored values.

    Windows drive and UNC paths compare case-insensitively and tolerate either
    slash style. POSIX paths remain case-sensitive. Blank values are treated as
    the global scope.
    """
    if project_dir is None:
        return None

    if not project_dir.strip():
        return None
    value: str = project_dir

    if _is_windows_project_dir(value):
        normalized: str = ntpath.normcase(ntpath.normpath(value))
        return f"windows:{normalized}"

    normalized = posixpath.normpath(value)
    return f"posix:{normalized}"


def is_read_scope_compatible(
    record_project_dir: str | None,
    requested_project_dir: str | None,
) -> bool:
    """Return whether a record may be read from an enforced project scope.

    Unscoped records remain globally readable for backward compatibility.
    Callers without a requested project are performing an unscoped read.
    """
    requested_scope: str | None = canonicalize_project_dir(requested_project_dir)
    if requested_scope is None:
        return True

    record_scope: str | None = canonicalize_project_dir(record_project_dir)
    return record_scope is None or record_scope == requested_scope


def is_recall_scope_compatible(
    record_project_dir: str | None,
    requested_project_dir: str | None,
) -> bool:
    """Return whether a record may be injected into project or global recall."""
    requested_scope: str | None = canonicalize_project_dir(requested_project_dir)
    record_scope: str | None = canonicalize_project_dir(record_project_dir)
    if requested_scope is None:
        return record_scope is None
    return record_scope is None or record_scope == requested_scope


def is_exact_scope_match(
    existing_project_dir: str | None,
    requested_project_dir: str | None,
) -> bool:
    """Return whether two records share the same deduplication scope."""
    return canonicalize_project_dir(existing_project_dir) == canonicalize_project_dir(
        requested_project_dir
    )
