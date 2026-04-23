"""Multi-signal scoring for memory retrieval.

Combines up to five signals into a single relevance score:

- **Semantic similarity** (alpha) — cosine similarity from vector search.
- **Recency** (beta) — exponential decay based on days since last access.
- **Frequency** (gamma) — log-scaled access count, rewarding reuse without
  letting high-frequency memories dominate.
- **Importance** (delta) — user-assigned or system-inferred importance rating.
- **Lexical overlap** (kappa) — fraction of query keywords present in the
  memory content+tags; 0 when no query is available (e.g. always-load recall).

Weights default to α=0.45, β=0.20, γ=0.10, δ=0.25, κ=0.0 and are tunable via
:class:`ScoringWeights` or the ``MEMORY_*`` environment variables in
:mod:`claude_memory.config`. The production default for ``kappa`` (read
from ``MEMORY_KAPPA``) is 0.30; leaving the dataclass default at 0.0 keeps
existing tests and semantic-only callers unchanged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScoringWeights:
    """Weights (and the recency-decay parameter) for the retrieval signals.

    The dataclass default sums to 1.0 with ``kappa=0`` so that adding the
    keyword-boost signal is opt-in. When ``kappa > 0`` the composite score
    may exceed 1.0, but relative ranking is preserved.

    ``recency_decay`` is a parameter of the recency signal, not a weight —
    it controls the per-day decay rate inside :func:`compute_recency_score`.
    Default 0.01 corresponds to ~69-day half-life. Lower values extend the
    effective memory lifetime (e.g. 0.002 ≈ 1-year half-life); 0.0 disables
    recency-based decay entirely in the retrieval pipeline.
    """

    alpha: float = 0.45  # semantic similarity
    beta: float = 0.20   # recency
    gamma: float = 0.10  # frequency
    delta: float = 0.25  # importance
    kappa: float = 0.0   # lexical overlap (opt-in keyword boost)
    recency_decay: float = 0.01  # per-day decay rate for the recency signal


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """A memory candidate annotated with its composite and per-signal scores."""

    memory_id: str
    score: float
    semantic_score: float
    recency_score: float
    frequency_score: float
    importance_score: float
    lexical_score: float = 0.0


# ---------------------------------------------------------------------------
# Individual signal functions
# ---------------------------------------------------------------------------

def compute_recency_score(days_since_access: float, decay_rate: float = 0.01) -> float:
    """Exponential decay based on time since last access.

    Half-life is approximately ``ln(2) / decay_rate ≈ 69.3`` days at the
    default rate of 0.01.

    Parameters
    ----------
    days_since_access:
        Non-negative number of days since the memory was last accessed.
    decay_rate:
        Controls how aggressively the score decays.  Higher values mean
        faster decay.

    Returns
    -------
    float
        A value in (0, 1] where 1.0 means "just accessed".
    """
    return math.exp(-decay_rate * max(days_since_access, 0.0))


def compute_frequency_score(access_count: int) -> float:
    """Log-scaled access count, capped at 1.0.

    Uses ``log₂(count + 1) / 10`` so that ~1023 accesses saturate the
    score.  This prevents frequently-accessed memories from dominating
    results while still rewarding reuse.

    Parameters
    ----------
    access_count:
        Total number of times the memory has been accessed (>= 0).

    Returns
    -------
    float
        A value in [0, 1].
    """
    return min(math.log2(max(access_count, 0) + 1) / 10.0, 1.0)


def compute_importance_score(importance: float) -> float:
    """Normalise a 0-10 importance value to the 0-1 range.

    Parameters
    ----------
    importance:
        Raw importance score stored in the database (0–10 scale).

    Returns
    -------
    float
        Clamped and normalised value in [0, 1].
    """
    return max(0.0, min(importance, 10.0)) / 10.0


# ---------------------------------------------------------------------------
# Combined scoring
# ---------------------------------------------------------------------------

def compute_combined_score(
    semantic_similarity: float,
    days_since_access: float,
    access_count: int,
    importance: float,
    weights: ScoringWeights = ScoringWeights(),
    *,
    memory_id: str = "",
    decay_rate: float = 0.01,
    lexical_score: float = 0.0,
) -> ScoredCandidate:
    """Compute a weighted multi-signal score for a single memory candidate.

    Each signal is independently normalised to [0, 1] before being combined
    via a weighted sum:

        ``score = α·semantic + β·recency + γ·frequency + δ·importance + κ·lexical``

    The ``κ·lexical`` term is the keyword-overlap boost: fraction of the
    query's meaningful tokens that appear in the memory's indexed text. When
    no query is available (e.g. session-start recall without a context
    string), callers pass ``lexical_score=0.0``.

    Parameters
    ----------
    semantic_similarity:
        Cosine similarity between the query embedding and the memory
        embedding, in [0, 1] (1 = identical).
    days_since_access:
        Days since the memory was last accessed.
    access_count:
        Total historical access count.
    importance:
        Raw importance rating on a 0–10 scale.
    weights:
        Signal weights.  Defaults to the standard weights (κ=0, no boost).
    memory_id:
        Identifier carried through to the returned :class:`ScoredCandidate`.
    decay_rate:
        Decay rate forwarded to :func:`compute_recency_score`.
    lexical_score:
        Fraction of query keywords present in the memory, in [0, 1].
        Defaults to 0.0 (no boost).

    Returns
    -------
    ScoredCandidate
        A fully-populated scored candidate.
    """
    sem: float = max(0.0, min(semantic_similarity, 1.0))
    # Prefer the weights-level recency_decay when the caller didn't override
    # via decay_rate (which defaults to 0.01 — the legacy hardcoded value).
    effective_decay: float = (
        weights.recency_decay if decay_rate == 0.01 else decay_rate
    )
    rec: float = compute_recency_score(days_since_access, decay_rate=effective_decay)
    freq: float = compute_frequency_score(access_count)
    imp: float = compute_importance_score(importance)
    lex: float = max(0.0, min(lexical_score, 1.0))

    combined: float = (
        weights.alpha * sem
        + weights.beta * rec
        + weights.gamma * freq
        + weights.delta * imp
        + weights.kappa * lex
    )

    return ScoredCandidate(
        memory_id=memory_id,
        score=combined,
        semantic_score=sem,
        recency_score=rec,
        frequency_score=freq,
        importance_score=imp,
        lexical_score=lex,
    )
