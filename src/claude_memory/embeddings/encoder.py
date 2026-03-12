"""Embedding encoder — thin, thread-safe wrapper around sentence-transformers."""

from __future__ import annotations

import threading
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

_DEFAULT_MODEL = "all-MiniLM-L6-v2"


class EmbeddingEncoder:
    """Lazy-loading, thread-safe encoder backed by a sentence-transformers model.

    The underlying ``SentenceTransformer`` is loaded on first call to
    :meth:`encode` or :meth:`encode_batch`, keeping import-time fast.

    Parameters
    ----------
    model_name:
        Any model identifier accepted by ``sentence_transformers.SentenceTransformer``.
        Defaults to ``all-MiniLM-L6-v2`` (384-dim, fast, good quality).
    """

    def __init__(self, model_name: str = _DEFAULT_MODEL) -> None:
        self._model_name = model_name
        self._model: SentenceTransformer | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_model(self) -> SentenceTransformer:
        """Load the model inside the lock (double-checked locking pattern)."""
        if self._model is None:
            with self._lock:
                # Re-check after acquiring the lock.
                if self._model is None:
                    try:
                        from sentence_transformers import SentenceTransformer
                    except ImportError as exc:
                        raise ImportError(
                            "sentence-transformers is required for embedding support. "
                            "Install it with:  pip install sentence-transformers"
                        ) from exc
                    self._model = SentenceTransformer(self._model_name)
        return self._model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def dimension(self) -> int:
        """Return the dimensionality of the embedding vectors produced by the model."""
        model = self._load_model()
        dimension: int = model.get_sentence_embedding_dimension()  # type: ignore[assignment]
        return dimension

    def encode(self, text: str) -> list[float]:
        """Encode a single piece of text into a unit-length embedding vector.

        Parameters
        ----------
        text:
            The text to encode.

        Returns
        -------
        list[float]
            A normalised (L2-norm = 1) embedding vector suitable for cosine
            similarity comparisons.
        """
        model = self._load_model()
        embedding = model.encode(text, normalize_embeddings=True)
        return embedding.tolist()  # type: ignore[union-attr]

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        """Encode multiple texts in one call (batched for efficiency).

        Parameters
        ----------
        texts:
            A list of strings to encode.

        Returns
        -------
        list[list[float]]
            One normalised embedding vector per input text.
        """
        if not texts:
            return []
        model = self._load_model()
        embeddings = model.encode(texts, normalize_embeddings=True)
        return embeddings.tolist()  # type: ignore[union-attr]


@lru_cache(maxsize=4)
def get_encoder(model_name: str = _DEFAULT_MODEL) -> EmbeddingEncoder:
    """Return a cached :class:`EmbeddingEncoder` singleton for *model_name*.

    Using :func:`functools.lru_cache` ensures at most one encoder instance
    per model name across the process lifetime.
    """
    return EmbeddingEncoder(model_name=model_name)
