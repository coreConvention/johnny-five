"""Configuration for claude-memory using pydantic-settings."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MemorySettings(BaseSettings):
    """All settings are overridable via MEMORY_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="MEMORY_")

    # Storage
    db_path: Path = Field(
        default=Path("~/.claude/memory.db"),
        description="Path to the SQLite database file.",
    )

    # Embedding model
    model_name: str = Field(
        default="all-MiniLM-L6-v2",
        description="Sentence-transformer model used for embeddings.",
    )
    embedding_dim: int = Field(
        default=384,
        description="Dimensionality of the embedding vectors (must match model).",
    )

    # Retrieval
    top_k: int = Field(
        default=15,
        description="Default number of results returned by search.",
    )
    dedup_threshold: float = Field(
        default=0.15,
        description="Cosine distance below which two memories are near-duplicates.",
    )

    # Multi-signal scoring weights
    # alpha+beta+gamma+delta = 1.0; kappa is an additive keyword-boost term
    # (0.30 default, matches the setting at which mempalace's LoCoMo R@10
    # jumps from 60% raw to 89% hybrid). When kappa > 0 the combined score
    # may exceed 1.0 but relative ranking is preserved.
    alpha: float = Field(default=0.45, description="Semantic similarity weight.")
    beta: float = Field(default=0.20, description="Recency weight.")
    gamma: float = Field(default=0.10, description="Frequency weight.")
    delta: float = Field(default=0.25, description="Importance weight.")
    kappa: float = Field(default=0.30, description="Lexical-overlap (keyword boost) weight.")

    # Decay
    decay_rate: float = Field(
        default=0.995,
        description="Daily importance decay multiplier.",
    )

    # Tiered lifecycle thresholds
    hot_access_threshold: int = Field(
        default=3,
        description="Minimum accesses in the last 30 days to remain in the hot tier.",
    )
    warm_days: int = Field(
        default=30,
        description="Days without access before demoting from hot to warm.",
    )
    cold_days: int = Field(
        default=180,
        description="Days without access before demoting from warm to cold.",
    )
    cold_importance_threshold: float = Field(
        default=3.0,
        description="Maximum importance score for cold-tier demotion.",
    )

    # Server
    server_port: int = Field(
        default=8787,
        description="Port for the SSE / HTTP transport.",
    )

    def resolve_db_path(self) -> Path:
        """Return *db_path* with ``~`` expanded to the user's home directory."""
        return self.db_path.expanduser()


@lru_cache(maxsize=1)
def get_settings() -> MemorySettings:
    """Return a cached singleton of :class:`MemorySettings`."""
    return MemorySettings()
