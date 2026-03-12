"""Importance decay and tier management for the memory lifecycle."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from claude_memory.db.queries import bulk_update_importance, update_tiers


@dataclass
class AgingReport:
    """Summary of a single aging cycle."""

    memories_decayed: int
    promoted_to_hot: int
    demoted_to_warm: int
    demoted_to_cold: int


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_importance_decay(
    conn: sqlite3.Connection,
    decay_rate: float = 0.995,
) -> int:
    """Decay importance for all non-archived memories not accessed in the last 24 h.

    Each qualifying memory's importance is multiplied by *decay_rate*,
    with a floor of 0.1 to prevent memories from reaching zero.

    The underlying SQL is roughly::

        UPDATE memories
        SET    importance = MAX(importance * :decay_rate, 0.1),
               updated_at = :now
        WHERE  tier != 'archived'
          AND  last_accessed < datetime('now', '-1 day');

    Parameters
    ----------
    conn:
        Open SQLite connection.
    decay_rate:
        Multiplicative factor applied once per cycle.  Values close
        to 1.0 produce slow decay; lower values decay faster.

    Returns
    -------
    int
        Number of memories whose importance was updated.
    """
    affected: int = bulk_update_importance(conn, decay_rate=decay_rate)
    return affected


def run_tier_updates(
    conn: sqlite3.Connection,
    hot_access_threshold: int = 3,
    warm_days: int = 30,
    cold_days: int = 180,
    cold_importance_threshold: float = 3.0,
) -> AgingReport:
    """Promote and demote memories between tiers based on access patterns.

    Tier rules
    ----------
    **Promotion to hot**
        A ``warm`` memory that has been accessed at least
        *hot_access_threshold* times in the last *warm_days* days is
        promoted back to ``hot``.

    **Demotion to warm**
        A ``hot`` memory that has not been accessed in
        *warm_days* days is demoted to ``warm``.

    **Demotion to cold**
        A ``warm`` memory that has not been accessed in *cold_days* days
        **and** whose importance is below *cold_importance_threshold* is
        demoted to ``cold``.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    hot_access_threshold:
        Minimum recent accesses required for promotion to hot.
    warm_days:
        Days without access before demoting hot to warm.
    cold_days:
        Days without access before demoting warm to cold.
    cold_importance_threshold:
        Maximum importance for cold-tier demotion.

    Returns
    -------
    AgingReport
        Counts of memories promoted/demoted in each direction.
    """
    promoted_hot, demoted_warm, demoted_cold = update_tiers(
        conn,
        hot_access_threshold=hot_access_threshold,
        warm_days=warm_days,
        cold_days=cold_days,
        cold_importance_threshold=cold_importance_threshold,
    )

    return AgingReport(
        memories_decayed=0,  # filled in by run_aging_cycle
        promoted_to_hot=promoted_hot,
        demoted_to_warm=demoted_warm,
        demoted_to_cold=demoted_cold,
    )


def run_aging_cycle(
    conn: sqlite3.Connection,
    decay_rate: float = 0.995,
    hot_access_threshold: int = 3,
    warm_days: int = 30,
    cold_days: int = 180,
    cold_importance_threshold: float = 3.0,
) -> AgingReport:
    """Run a full aging cycle: importance decay followed by tier updates.

    This is the top-level function that should be called periodically
    (e.g. once per day via a scheduled task or at the start of a
    session).

    Parameters
    ----------
    conn:
        Open SQLite connection.
    decay_rate:
        Daily importance decay multiplier (see :func:`run_importance_decay`).
    hot_access_threshold:
        Minimum accesses for promotion to hot tier.
    warm_days:
        Idle days before hot → warm demotion.
    cold_days:
        Idle days before warm → cold demotion.
    cold_importance_threshold:
        Maximum importance for cold-tier demotion.

    Returns
    -------
    AgingReport
        Combined results of the decay and tier-update passes.
    """
    decayed: int = run_importance_decay(conn, decay_rate=decay_rate)

    report: AgingReport = run_tier_updates(
        conn,
        hot_access_threshold=hot_access_threshold,
        warm_days=warm_days,
        cold_days=cold_days,
        cold_importance_threshold=cold_importance_threshold,
    )
    report.memories_decayed = decayed

    return report
