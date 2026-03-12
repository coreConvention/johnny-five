"""Memory lifecycle — aging, consolidation, deduplication."""

from claude_memory.lifecycle.aging import AgingReport, run_aging_cycle
from claude_memory.lifecycle.consolidation import ConsolidationReport, run_consolidation
from claude_memory.lifecycle.dedup import DedupResult, store_with_dedup

__all__ = [
    "AgingReport",
    "ConsolidationReport",
    "DedupResult",
    "run_aging_cycle",
    "run_consolidation",
    "store_with_dedup",
]
