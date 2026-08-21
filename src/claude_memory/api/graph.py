"""3D spatial projection + derived relationship graph for the dashboard.

The corpus has no explicit edges to draw: ``memories.supersedes`` and
``memories.consolidated_from`` are real directed edges *by design*, but they are
written only by an explicitly-confirmed reconciliation, so on a corpus where
nobody has run one they are empty on every row. Every relationship rendered here
is therefore **derived** — from the embeddings, the tags, the project scope, or
the creation timestamps.

Two invariants shape this module:

1. **Positions are computed once, on the whole corpus.** Filtering happens in
   the browser and only changes *visibility*. If a filter re-ran the projection,
   points would jump between views and destroy the spatial memory that makes the
   view worth having in the first place.

2. **Reading never writes.** ``access_count`` feeds the frequency term of the
   retrieval scorer, so a view that bumped it while you browsed would silently
   distort the ranking it exists to expose. Everything here is ``SELECT``-only,
   the same discipline :func:`tool_memory_why` already follows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from claude_memory.db.queries import MemoryRecord
from claude_memory.lifecycle.consolidation import _read_embeddings
from claude_memory.scope import canonicalize_project_dir, project_dir_basename

logger = logging.getLogger(__name__)

# Projection methods. "pca" is kept as a fast debug option and is deliberately
# labelled low-quality in the UI: on 384-dim sentence embeddings it preserves
# only ~5% of each point's nearest-neighbour set (measured), which renders as a
# plausible-looking but meaningless blob.
PROJECTIONS: tuple[str, ...] = ("tsne", "umap", "pca")
EDGE_MODES: tuple[str, ...] = ("semantic", "tags", "project", "time", "none")

# Intermediate dimensionality before the non-linear step. Standard practice:
# it strips noise and makes t-SNE/UMAP dramatically faster without discarding
# neighbourhood structure (50 components retain ~63% variance on this corpus).
_PCA_DIMS: int = 50

# A hard ceiling on returned edges. Past roughly this many lines the render
# stops being readable long before it stops being fast, so the cap is about
# legibility, not performance. Truncation is always reported, never silent.
_MAX_EDGES: int = 30_000

_PREVIEW_CHARS: int = 140

# Bumped whenever the layout maths changes (projection params, normalisation).
# The cache key is otherwise purely corpus membership, so without this a code
# change would keep serving coordinates computed by the previous algorithm —
# silently, since a stale layout still looks perfectly plausible.
_LAYOUT_VERSION: int = 2

# Tags above this document frequency are dropped from the tag-similarity space.
# `lifecycle:active` sits on 27% of the corpus; left in, it alone would imply
# ~314k pairs and turn the view into a hairball that says nothing.
_TAG_MAX_DF_RATIO: float = 0.05

# Two memories written this close together are treated as the same burst of
# work. `source_session` would be the principled key, but it is populated on
# only ~1% of rows, so wall-clock proximity is the usable signal.
_TIME_WINDOW_SECONDS: float = 30 * 60


class GraphError(ValueError):
    """Raised for an unusable request (unknown mode, missing optional dep)."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _load_records(conn: sqlite3.Connection) -> list[MemoryRecord]:
    """Read every memory, archived included, ordered for a stable layout.

    Ordering by ``id`` (ULIDs, so creation-ordered) keeps the row order — and
    therefore the projection's random init — identical between runs, which is
    what makes a cached layout reproducible.
    """
    from claude_memory.db.queries import _row_to_record

    rows = conn.execute("SELECT * FROM memories ORDER BY id").fetchall()
    return [_row_to_record(row) for row in rows]


def _fingerprint(ids: list[str]) -> str:
    """Identify the corpus by *membership*, for cache invalidation.

    Deliberately built from ids alone. ``updated_at`` is excluded because the
    daily aging cycle rewrites it on every row (``queries.py`` decay pass) —
    keying on it would invalidate the cache every day for a change that cannot
    move a single point.
    """
    digest = hashlib.sha1("\n".join(ids).encode("utf-8")).hexdigest()  # noqa: S324
    return f"{len(ids)}-{digest[:16]}"


def _embedding_matrix(
    conn: sqlite3.Connection, records: list[MemoryRecord]
) -> tuple[list[MemoryRecord], np.ndarray]:
    """Return the records that have a usable embedding, plus their matrix.

    Records without a vector are dropped rather than zero-filled: a zero row
    would land at the origin and read as a real cluster.
    """
    ids = [r.id for r in records]
    raw = _read_embeddings(conn, ids)

    vectors: list[np.ndarray] = []
    kept: list[MemoryRecord] = []
    dims: dict[int, int] = {}
    for record in records:
        vec = raw.get(record.id)
        if vec is None:
            continue
        dims[len(vec)] = dims.get(len(vec), 0) + 1
        vectors.append(np.asarray(vec, dtype=np.float32))
        kept.append(record)

    if not vectors:
        return [], np.zeros((0, 0), dtype=np.float32)

    # Guard against a mixed-dimension corpus (only possible if the embedding
    # model was swapped mid-life). Keep the dominant width; a ragged stack
    # would raise deep inside numpy with a far less obvious message.
    dominant = max(dims, key=lambda d: dims[d])
    if len(dims) > 1:
        logger.warning(
            "Mixed embedding dimensions %s; keeping %d-dim vectors", dims, dominant
        )
        filtered = [(r, v) for r, v in zip(kept, vectors) if v.shape[0] == dominant]
        kept = [r for r, _ in filtered]
        vectors = [v for _, v in filtered]

    matrix = np.vstack(vectors)
    # Vectors are written L2-normalised by the encoder, so a dot product is
    # already cosine. Re-normalise defensively — a non-unit row would silently
    # inflate every similarity it takes part in.
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return kept, matrix / norms


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def _project(matrix: np.ndarray, method: str) -> np.ndarray:
    """Reduce an (N, D) embedding matrix to (N, 3) display coordinates."""
    if method not in PROJECTIONS:
        raise GraphError(f"Unknown projection {method!r}; expected one of {PROJECTIONS}")

    n_samples = matrix.shape[0]
    if n_samples < 4:
        # Too few points for any meaningful reduction; pad out to 3 columns.
        padded = np.zeros((n_samples, 3), dtype=np.float32)
        padded[:, : min(3, matrix.shape[1])] = matrix[:, :3]
        return padded

    from sklearn.decomposition import PCA

    if method == "pca":
        return PCA(n_components=3, random_state=42).fit_transform(matrix)

    # Both non-linear methods run on a PCA pre-reduction rather than the raw
    # 384 dims: much faster, and it denoises without losing local structure.
    pre_dims = min(_PCA_DIMS, n_samples - 1, matrix.shape[1])
    reduced = PCA(n_components=pre_dims, random_state=42).fit_transform(matrix)

    if method == "umap":
        try:
            import umap  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - depends on image build
            raise GraphError(
                "UMAP is not installed in this image. Rebuild with umap-learn "
                "(`docker compose build johnny-five-dashboard`) or use "
                "projection=tsne."
            ) from exc
        reducer = umap.UMAP(
            n_components=3,
            n_neighbors=15,
            min_dist=0.1,
            metric="cosine",
            random_state=42,
        )
        return reducer.fit_transform(reduced)

    from sklearn.manifold import TSNE

    return TSNE(
        n_components=3,
        perplexity=min(30.0, max(5.0, (n_samples - 1) / 3.0)),
        init="pca",
        learning_rate="auto",
        random_state=42,
    ).fit_transform(reduced)


def _normalize_positions(coords: np.ndarray) -> np.ndarray:
    """Centre and scale coordinates into a predictable world scale.

    The renderer's camera framing, point sizes and edge widths are all tuned in
    world units, so the scene must not depend on a projection's arbitrary output
    scale (t-SNE spans ~±30, PCA ~±0.5).

    Scaled on the 98th-percentile radius rather than the maximum: a handful of
    semantic outliers sit far outside the bulk of the corpus, and dividing by
    the true maximum lets those few points shrink everything else into an
    unreadable knot in the middle of the screen. Outliers simply extend past the
    nominal radius instead, which is the honest rendering — they *are* outliers.
    """
    if coords.size == 0:
        return coords
    centred = coords - coords.mean(axis=0)
    radii = np.linalg.norm(centred, axis=1)
    reference = float(np.percentile(radii, 98)) if radii.size else 0.0
    if reference <= 0:
        reference = float(radii.max()) if radii.size else 0.0
    if reference <= 0:
        return centred
    return centred * (100.0 / reference)


# ---------------------------------------------------------------------------
# Edge builders — every mode returns [(i, j, weight)] with i < j
# ---------------------------------------------------------------------------


def _dedupe_edges(raw: Iterable[tuple[int, int, float]]) -> list[tuple[int, int, float]]:
    """Collapse duplicate undirected pairs, keeping the strongest weight."""
    best: dict[tuple[int, int], float] = {}
    for i, j, w in raw:
        if i == j:
            continue
        key = (i, j) if i < j else (j, i)
        if w > best.get(key, -math.inf):
            best[key] = w
    return [(i, j, w) for (i, j), w in best.items()]


def _knn_edges(
    matrix: np.ndarray,
    k: int,
    threshold: float,
    *,
    restrict: np.ndarray | None = None,
    chunk: int = 512,
) -> list[tuple[int, int, float]]:
    """Top-*k* cosine neighbours per node, kept only above *threshold*.

    Top-k *then* threshold, rather than threshold alone, is what keeps this
    readable: a flat cosine cut produces wildly uneven degree (>112k edges at
    0.5 on this corpus, 120 at 0.8), whereas per-node top-k bounds the result at
    N·k/2 and gives dense and sparse regions comparable visual weight.

    Similarities are computed in row chunks so peak memory stays at
    ``chunk × N`` instead of materialising the full N×N matrix (68 MB at
    N=2932, and quadratic from there).

    *restrict*, when given, is a per-node group label; only same-group pairs are
    considered.
    """
    n = matrix.shape[0]
    if n < 2 or k < 1:
        return []

    kk = min(k, n - 1)
    out: list[tuple[int, int, float]] = []

    for start in range(0, n, chunk):
        end = min(start + chunk, n)
        sims = matrix[start:end] @ matrix.T

        # Exclude self-pairs without disturbing the argpartition.
        rows = np.arange(end - start)
        sims[rows, np.arange(start, end)] = -np.inf

        if restrict is not None:
            block = restrict[start:end][:, None] == restrict[None, :]
            sims = np.where(block, sims, -np.inf)

        idx = np.argpartition(-sims, kk - 1, axis=1)[:, :kk]
        for row in range(end - start):
            source = start + row
            for col in idx[row]:
                weight = float(sims[row, col])
                if weight >= threshold and np.isfinite(weight):
                    out.append((source, int(col), weight))

    return _dedupe_edges(out)


def _tag_edges(
    records: list[MemoryRecord], k: int
) -> list[tuple[int, int, float]]:
    """Connect memories that share *distinctive* tags.

    Tags are turned into an IDF-weighted sparse vector per memory and compared
    by cosine, so agreeing on a rare tag counts for far more than agreeing on a
    ubiquitous one. Hub tags above :data:`_TAG_MAX_DF_RATIO` are dropped
    outright — they describe the corpus, not any relationship within it.
    """
    n = len(records)
    if n < 2:
        return []

    from scipy import sparse

    doc_freq: dict[str, int] = {}
    per_record: list[list[str]] = []
    for record in records:
        tags = record.tags if isinstance(record.tags, list) else json.loads(record.tags or "[]")
        unique = sorted({str(t) for t in tags if str(t).strip()})
        per_record.append(unique)
        for tag in unique:
            doc_freq[tag] = doc_freq.get(tag, 0) + 1

    max_df = max(2, int(n * _TAG_MAX_DF_RATIO))
    vocabulary = {t: i for i, t in enumerate(sorted(t for t, df in doc_freq.items() if df <= max_df))}
    if not vocabulary:
        return []

    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []
    for row, tags in enumerate(per_record):
        for tag in tags:
            col = vocabulary.get(tag)
            if col is None:
                continue
            rows.append(row)
            cols.append(col)
            vals.append(math.log(n / (1 + doc_freq[tag])))

    if not rows:
        return []

    matrix = sparse.csr_matrix(
        (vals, (rows, cols)), shape=(n, len(vocabulary)), dtype=np.float32
    )
    # Row-normalise so the dot product below is a cosine, not a magnitude
    # contest between heavily- and lightly-tagged memories.
    norms = np.sqrt(matrix.multiply(matrix).sum(axis=1)).A.ravel()
    norms[norms == 0] = 1.0
    matrix = sparse.diags(1.0 / norms) @ matrix

    similarity = (matrix @ matrix.T).tocoo()

    # Keep the top-k partners per node. The sparse product is already small
    # once hub tags are gone, so this runs over the non-zeros directly.
    by_row: dict[int, list[tuple[float, int]]] = {}
    for i, j, v in zip(similarity.row, similarity.col, similarity.data):
        if i == j or v <= 0:
            continue
        by_row.setdefault(int(i), []).append((float(v), int(j)))

    out: list[tuple[int, int, float]] = []
    for i, partners in by_row.items():
        partners.sort(reverse=True)
        for weight, j in partners[:k]:
            out.append((i, j, weight))
    return _dedupe_edges(out)


def _project_edges(
    records: list[MemoryRecord], matrix: np.ndarray, k: int
) -> list[tuple[int, int, float]]:
    """Link each memory to its nearest neighbours *from the same project*.

    A literal "same project = connected" reading would be N² — w31rd.com alone
    holds 2,145 memories, i.e. 2.3M pairs — so this restricts the semantic kNN
    to same-project pairs instead. The result shows how each project's knowledge
    is internally organised, and stays bounded.

    Project scope is canonicalised first: the raw column holds both
    ``Z:/Personal/...`` and ``Z:\\Personal\\...`` for the same project, which
    would otherwise render as two unrelated groups.
    """
    labels = np.array(
        [canonicalize_project_dir(r.project_dir) or "\u0000global" for r in records]
    )
    # argpartition needs a numeric label array for the broadcast comparison.
    unique = {label: index for index, label in enumerate(sorted(set(labels.tolist())))}
    numeric = np.array([unique[label] for label in labels.tolist()], dtype=np.int32)
    return _knn_edges(matrix, k=k, threshold=-1.0, restrict=numeric)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _time_edges(records: list[MemoryRecord]) -> list[tuple[int, int, float]]:
    """Chain memories created within the same burst of work.

    Consecutive-in-time only, so this is at most N-1 edges and reads as threads
    rather than a cloud. Uses ``created_at``: ``updated_at`` is rewritten by the
    aging cycle and would collapse the whole corpus onto today.
    """
    stamped = [
        (parsed, index)
        for index, record in enumerate(records)
        if (parsed := _parse_iso(record.created_at)) is not None
    ]
    stamped.sort()

    out: list[tuple[int, int, float]] = []
    for (earlier, i), (later, j) in zip(stamped, stamped[1:]):
        gap = (later - earlier).total_seconds()
        if gap <= _TIME_WINDOW_SECONDS:
            # Nearer in time reads as a stronger link.
            out.append((i, j, 1.0 - (gap / _TIME_WINDOW_SECONDS)))
    return _dedupe_edges(out)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _node_payload(
    record: MemoryRecord, position: np.ndarray
) -> dict[str, Any]:
    """Serialize one node — coordinates plus what the UI needs to draw/filter.

    Carries only a short preview. Full content averages 2.7 KB per memory
    (7.8 MB across the corpus), so shipping it here would make the payload
    ~18× larger for text that is only ever read one record at a time; the
    detail endpoint fetches it on click instead.
    """
    tags = record.tags if isinstance(record.tags, list) else json.loads(record.tags or "[]")
    content = record.content or ""
    preview = content[:_PREVIEW_CHARS].replace("\n", " ").strip()
    if len(content) > _PREVIEW_CHARS:
        preview += "\u2026"

    scope = canonicalize_project_dir(record.project_dir)
    return {
        "id": record.id,
        "x": round(float(position[0]), 2),
        "y": round(float(position[1]), 2),
        "z": round(float(position[2]), 2),
        "type": record.type,
        "tier": record.tier,
        "importance": round(float(record.importance), 2),
        "access_count": record.access_count,
        "project": project_dir_basename(record.project_dir) if record.project_dir else None,
        "scope": scope,
        "created_at": record.created_at,
        "tags": tags[:8],
        "preview": preview,
    }


def _build_edges(
    mode: str,
    records: list[MemoryRecord],
    matrix: np.ndarray,
    *,
    threshold: float,
    k: int,
) -> list[tuple[int, int, float]]:
    if mode == "none":
        return []
    if mode == "semantic":
        return _knn_edges(matrix, k=k, threshold=threshold)
    if mode == "tags":
        return _tag_edges(records, k=k)
    if mode == "project":
        return _project_edges(records, matrix, k=k)
    if mode == "time":
        return _time_edges(records)
    raise GraphError(f"Unknown edge mode {mode!r}; expected one of {EDGE_MODES}")


class _LayoutCache:
    """Memoise projections, in memory and on disk.

    t-SNE costs ~5 s for this corpus and UMAP's first call pays a numba JIT on
    top, which is far too slow to sit in front of a view toggle. Disk backing
    means a container restart doesn't re-pay it either.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._memory: dict[str, np.ndarray] = {}

    def _path(self, key: str) -> Path:
        return self._directory / f"{key}.json"

    def get(self, key: str) -> np.ndarray | None:
        cached = self._memory.get(key)
        if cached is not None:
            return cached
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            coords = np.asarray(json.loads(path.read_text("utf-8")), dtype=np.float32)
        except (OSError, ValueError):
            logger.warning("Discarding unreadable layout cache %s", path)
            return None
        self._memory[key] = coords
        return coords

    def put(self, key: str, coords: np.ndarray) -> None:
        self._memory[key] = coords
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            self._path(key).write_text(
                json.dumps(np.asarray(coords, dtype=float).round(3).tolist()), "utf-8"
            )
        except OSError as exc:
            # A read-only or full volume must not break the view; the in-memory
            # entry still serves this process.
            logger.warning("Could not persist layout cache: %s", exc)


_cache: _LayoutCache | None = None


def _get_cache(db_path: Path) -> _LayoutCache:
    global _cache
    if _cache is None:
        _cache = _LayoutCache(db_path.parent / "_graph_cache")
    return _cache


def build_graph(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    projection: str = "tsne",
    edges: str = "semantic",
    threshold: float = 0.6,
    k: int = 8,
) -> dict[str, Any]:
    """Assemble the full node/edge payload for the 3D view.

    Always covers the entire corpus, archived rows included — filtering is the
    browser's job (see the module docstring's first invariant). The response
    reports its own cost and any edge truncation so the UI can be honest about
    what it is showing.
    """
    if edges not in EDGE_MODES:
        raise GraphError(f"Unknown edge mode {edges!r}; expected one of {EDGE_MODES}")
    # Validate the projection HERE, not where it is first used. It becomes a
    # path segment in the cache key below, and _project() only rejects an
    # unknown value further down -- so an unvalidated string would reach the
    # filesystem lookup before anything checked it.
    if projection not in PROJECTIONS:
        raise GraphError(
            f"Unknown projection {projection!r}; expected one of {PROJECTIONS}"
        )

    started = time.perf_counter()
    records = _load_records(conn)
    records, matrix = _embedding_matrix(conn, records)

    if not records:
        return {
            "nodes": [],
            "edges": [],
            "meta": {
                "projection": projection,
                "edge_mode": edges,
                "node_count": 0,
                "edge_count": 0,
                "truncated": False,
                "cached": False,
                "elapsed_ms": 0,
            },
        }

    key = f"v{_LAYOUT_VERSION}-{projection}-{_fingerprint([r.id for r in records])}"
    cache = _get_cache(db_path)
    coords = cache.get(key)
    was_cached = coords is not None and coords.shape[0] == len(records)

    if not was_cached:
        coords = _normalize_positions(np.asarray(_project(matrix, projection)))
        cache.put(key, coords)

    assert coords is not None  # narrowed by the branch above

    edge_list = _build_edges(
        edges, records, matrix, threshold=threshold, k=k
    )
    total_edges = len(edge_list)
    # Strongest-first, so truncation drops the weakest relationships rather than
    # an arbitrary slice.
    edge_list.sort(key=lambda e: e[2], reverse=True)
    truncated = total_edges > _MAX_EDGES
    edge_list = edge_list[:_MAX_EDGES]

    return {
        "nodes": [_node_payload(r, coords[i]) for i, r in enumerate(records)],
        "edges": [[i, j, round(w, 3)] for i, j, w in edge_list],
        "meta": {
            "projection": projection,
            "edge_mode": edges,
            "threshold": threshold,
            "k": k,
            "node_count": len(records),
            "edge_count": len(edge_list),
            "total_edges": total_edges,
            "truncated": truncated,
            "cached": was_cached,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        },
    }
