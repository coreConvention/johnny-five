"""Tests for the 3D memory graph (``claude_memory.api.graph``).

The load-bearing tests here are in :class:`TestProjectionQuality`. Every other
part of this feature can be checked by reading its output, but a projection
cannot: a bad layout still renders as a perfectly plausible cloud of points. PCA
on sentence embeddings preserves only a few percent of each point's neighbourhood
and looks fine on screen, so layout quality is asserted numerically or not at all.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from claude_memory.api import graph as G
from claude_memory.db.queries import MemoryRecord, insert_memory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clustered_embeddings(
    n_clusters: int = 20, per_cluster: int = 15, dim: int = 384, seed: int = 7
) -> np.ndarray:
    """Build unit-norm vectors in *n_clusters* well-separated groups.

    Many small clusters on purpose: three principal components cannot separate
    twenty groups, so this is a structure a variance-maximising projection is
    expected to fail on and a neighbourhood-preserving one is expected to hold.
    """
    rng = np.random.default_rng(seed)
    centres = rng.normal(size=(n_clusters, dim))
    centres /= np.linalg.norm(centres, axis=1, keepdims=True)
    points = np.repeat(centres, per_cluster, axis=0)
    points = points + rng.normal(scale=0.05, size=points.shape)
    return (points / np.linalg.norm(points, axis=1, keepdims=True)).astype(np.float32)


def _knn_preservation(high: np.ndarray, low: np.ndarray, k: int = 10) -> float:
    """Fraction of each point's *k* nearest neighbours that survive projection."""
    sims = high @ high.T
    np.fill_diagonal(sims, -np.inf)
    true_nn = np.argpartition(-sims, k, axis=1)[:, :k]

    dist = ((low[:, None, :] - low[None, :, :]) ** 2).sum(-1)
    np.fill_diagonal(dist, np.inf)
    proj_nn = np.argpartition(dist, k, axis=1)[:, :k]

    return float(np.mean([len(set(a) & set(b)) / k for a, b in zip(true_nn, proj_nn)]))


def _record(mid: str, **overrides) -> MemoryRecord:
    now = datetime.now(timezone.utc).isoformat()
    fields = dict(
        id=mid, content=f"content for {mid}", summary=None, type="lesson", tags=[],
        created_at=now, updated_at=now, last_accessed=now, access_count=0,
        importance=5.0, tier="hot", project_dir=None, source_session=None,
        supersedes=None, consolidated_from=[], metadata={},
    )
    fields.update(overrides)
    return MemoryRecord(**fields)


def _insert(conn: sqlite3.Connection, mid: str, embedding: list[float], **overrides) -> None:
    insert_memory(conn, _record(mid, **overrides), embedding)


class _NoCloseConn:
    """Keeps an in-memory DB alive past a tool's ``finally: conn.close()``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


@pytest.fixture()
def clustered_db(db_conn: sqlite3.Connection):
    """A corpus of 300 memories in 20 semantic clusters."""
    vectors = _clustered_embeddings()
    for i, vec in enumerate(vectors):
        _insert(db_conn, f"mem-{i:04d}", vec.tolist())
    return db_conn, vectors


@pytest.fixture(autouse=True)
def _isolate_layout_cache():
    """The layout cache is module-level; never let it leak between tests."""
    G._cache = None
    yield
    G._cache = None


# ---------------------------------------------------------------------------
# Projection quality — the regression guard for the whole feature
# ---------------------------------------------------------------------------


class TestProjectionQuality:
    def test_tsne_preserves_neighbourhoods(self) -> None:
        """t-SNE must keep most of each point's nearest neighbours.

        This is the assertion that makes the view trustworthy. If it regresses
        the graph still draws — it just quietly stops meaning anything.
        """
        high = _clustered_embeddings()
        low = np.asarray(G._project(high, "tsne"))
        assert _knn_preservation(high, low) >= 0.5

    def test_pca_is_measurably_worse_than_tsne(self) -> None:
        """Pins the choice of default projection, not merely its quality.

        PCA is offered in the UI as a fast option and labelled low-quality; this
        records the reason so nobody promotes it to the default.

        Uses many small clusters, because three principal components can in fact
        separate a handful of well-spaced groups. Note the margin here badly
        understates reality: tidy Gaussian clusters are far kinder to PCA than
        real sentence embeddings, where the measured gap is ~42% vs ~5% — nearly
        nine-fold. The assertion is deliberately loose enough to survive that
        difference rather than encode a number this data cannot support.
        """
        high = _clustered_embeddings(n_clusters=60, per_cluster=5)
        tsne = _knn_preservation(high, np.asarray(G._project(high, "tsne")))
        pca = _knn_preservation(high, np.asarray(G._project(high, "pca")))
        assert tsne > pca * 1.25

    def test_positions_are_normalised_to_a_predictable_scale(self) -> None:
        coords = G._normalize_positions(
            np.asarray(G._project(_clustered_embeddings(), "pca"))
        )
        assert np.allclose(coords.mean(axis=0), 0, atol=1e-3)
        # Scaled on the 98th percentile, so a few outliers may exceed 100.
        assert 60 < float(np.percentile(np.linalg.norm(coords, axis=1), 98)) < 140

    def test_outliers_do_not_shrink_the_bulk(self) -> None:
        """One far-flung point must not collapse everything else to the centre."""
        rng = np.random.default_rng(3)
        coords = rng.normal(size=(200, 3))
        coords[0] = [5000.0, 5000.0, 5000.0]
        bulk = np.linalg.norm(G._normalize_positions(coords)[1:], axis=1)
        assert float(np.median(bulk)) > 20.0

    def test_unknown_projection_is_rejected(self) -> None:
        with pytest.raises(G.GraphError):
            G._project(_clustered_embeddings(), "nope")

    def test_tiny_corpus_does_not_crash(self) -> None:
        """Fewer points than a projection needs must degrade, not explode."""
        assert G._project(np.eye(3, 384, dtype=np.float32), "tsne").shape == (3, 3)


class TestUmapDegradesGracefully:
    def test_missing_umap_raises_a_helpful_error(self, monkeypatch) -> None:
        """UMAP is optional; its absence must be a 400, not a 500.

        The dependency is heavy (numba + llvmlite), so an image built without it
        has to stay usable rather than failing opaquely. A ``None`` entry in
        ``sys.modules`` is the documented way to make an import raise.
        """
        monkeypatch.setitem(sys.modules, "umap", None)
        with pytest.raises(G.GraphError, match="umap-learn"):
            G._project(_clustered_embeddings(n_clusters=3, per_cluster=5), "umap")


# ---------------------------------------------------------------------------
# Edge derivation
# ---------------------------------------------------------------------------


class TestSemanticEdges:
    def test_edges_are_undirected_deduped_and_bounded(self) -> None:
        matrix = _clustered_embeddings()
        edges = G._knn_edges(matrix, k=6, threshold=0.3)
        pairs = {(i, j) for i, j, _ in edges}
        assert edges, "fixture should produce edges at this threshold"
        assert all(i < j for i, j, _ in edges), "edges must be canonically ordered"
        assert len(pairs) == len(edges), "no duplicate undirected pairs"
        assert len(edges) <= matrix.shape[0] * 6, "top-k must bound the edge count"

    def test_raising_the_threshold_only_removes_edges(self) -> None:
        """The slider must be monotonic — strictly a filter, never a re-query."""
        matrix = _clustered_embeddings()
        all_edges = G._knn_edges(matrix, k=8, threshold=0.0)
        loose = {(i, j) for i, j, _ in all_edges}

        # Cut at the median weight rather than a hard-coded similarity: the
        # fixture's absolute cosine range is an artefact of its noise scale, so
        # a fixed number would silently stop testing anything if that changed.
        median = float(np.median([w for _, _, w in all_edges]))
        tight = {(i, j) for i, j, _ in G._knn_edges(matrix, k=8, threshold=median)}

        assert tight, "threshold should not empty the graph outright"
        assert tight <= loose, "raising the threshold must only ever remove edges"
        assert len(tight) < len(loose)

    def test_neighbours_come_from_the_same_cluster(self) -> None:
        """Checks the edges mean something, not merely that they exist."""
        matrix = _clustered_embeddings(n_clusters=20, per_cluster=15)
        edges = G._knn_edges(matrix, k=5, threshold=0.3)
        assert edges
        same = sum(1 for i, j, _ in edges if i // 15 == j // 15)
        assert same / len(edges) > 0.95

    def test_chunking_does_not_change_the_result(self) -> None:
        """The row-chunked similarity pass must be a pure optimisation.

        It exists to keep peak memory at chunk x N instead of materialising the
        full N x N matrix, which is quadratic and would dominate on a large corpus.
        """
        matrix = _clustered_embeddings(n_clusters=6, per_cluster=8)
        whole = sorted(
            (i, j) for i, j, _ in G._knn_edges(matrix, k=4, threshold=0.0, chunk=10_000)
        )
        chunked = sorted(
            (i, j) for i, j, _ in G._knn_edges(matrix, k=4, threshold=0.0, chunk=7)
        )
        assert whole == chunked


class TestTagEdges:
    def test_ubiquitous_tags_do_not_create_edges(self) -> None:
        """A tag on most of the corpus describes the corpus, not a relationship.

        Without this, `lifecycle:active` alone (27% of the real corpus) would
        imply hundreds of thousands of pairs and drown every genuine signal.
        """
        records = [_record(f"m{i}", tags=["lifecycle:active"]) for i in range(50)]
        assert G._tag_edges(records, k=8) == []

    def test_a_rare_shared_tag_creates_an_edge(self) -> None:
        records = [_record(f"m{i}", tags=["lifecycle:active"]) for i in range(50)]
        records[0].tags = ["lifecycle:active", "very-rare-tag"]
        records[1].tags = ["lifecycle:active", "very-rare-tag"]
        assert (0, 1) in {(i, j) for i, j, _ in G._tag_edges(records, k=8)}

    def test_untagged_corpus_yields_no_edges(self) -> None:
        assert G._tag_edges([_record(f"m{i}") for i in range(10)], k=8) == []


class TestProjectEdges:
    def test_slash_styles_merge_and_projects_never_cross(self) -> None:
        """`Z:/Personal/x` and `Z:\\Personal\\x` are one project, not two.

        The column is stored raw and unnormalised, so without canonicalisation
        one project renders as two unconnected groups.
        """
        matrix = _clustered_embeddings(n_clusters=2, per_cluster=6)
        dirs = (
            ["Z:/Personal/alpha"] * 3
            + ["Z:\\Personal\\alpha"] * 3
            + ["Z:/Personal/beta"] * 6
        )
        records = [_record(f"m{i}", project_dir=d) for i, d in enumerate(dirs)]

        edges = G._project_edges(records, matrix, k=4)
        assert edges, "expected within-project links"
        for i, j, _ in edges:
            assert (i < 6) == (j < 6), "linked a memory across two projects"
        # The two slash styles must actually be joined, not merely not-crossed.
        assert any(i < 3 <= j < 6 for i, j, _ in edges)


class TestTimeEdges:
    @staticmethod
    def _at(offsets_minutes: list[float]) -> list[MemoryRecord]:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return [
            _record(f"m{i}", created_at=(base + timedelta(minutes=off)).isoformat())
            for i, off in enumerate(offsets_minutes)
        ]

    def test_links_only_within_the_burst_window(self) -> None:
        # Three in one burst, a long gap, then two more.
        pairs = {(i, j) for i, j, _ in G._time_edges(self._at([0, 5, 10, 6000, 6005]))}
        assert pairs == {(0, 1), (1, 2), (3, 4)}

    def test_closer_in_time_is_a_stronger_link(self) -> None:
        near = G._time_edges(self._at([0, 1]))[0][2]
        far = G._time_edges(self._at([0, 25]))[0][2]
        assert near > far

    def test_unparseable_timestamps_are_skipped_not_fatal(self) -> None:
        records = self._at([0, 5])
        records[0].created_at = "not-a-date"
        assert G._time_edges(records) == []


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestLayoutCache:
    def test_fingerprint_ignores_updated_at(self, clustered_db, tmp_path) -> None:
        """The aging cycle rewrites `updated_at` on every row, daily.

        Keying the cache on it would discard a valid layout every night for a
        change that cannot move a single point.
        """
        conn, _ = clustered_db
        first = G.build_graph(conn, tmp_path / "memory.db", projection="pca")
        conn.execute("UPDATE memories SET updated_at = '2099-01-01T00:00:00+00:00'")
        second = G.build_graph(conn, tmp_path / "memory.db", projection="pca")
        assert second["meta"]["cached"] is True
        assert [n["x"] for n in first["nodes"]] == [n["x"] for n in second["nodes"]]

    def test_membership_change_invalidates(self, clustered_db, tmp_path) -> None:
        conn, vectors = clustered_db
        G.build_graph(conn, tmp_path / "memory.db", projection="pca")
        _insert(conn, "mem-new", vectors[0].tolist())
        result = G.build_graph(conn, tmp_path / "memory.db", projection="pca")
        assert result["meta"]["cached"] is False

    def test_cache_survives_a_new_process(self, clustered_db, tmp_path) -> None:
        """A restart must not re-pay a multi-second projection."""
        conn, _ = clustered_db
        G.build_graph(conn, tmp_path / "memory.db", projection="pca")
        G._cache = None  # simulate a fresh process reading the on-disk cache
        result = G.build_graph(conn, tmp_path / "memory.db", projection="pca")
        assert result["meta"]["cached"] is True

    def test_projections_do_not_share_a_cache_entry(self, clustered_db, tmp_path) -> None:
        conn, _ = clustered_db
        pca = G.build_graph(conn, tmp_path / "memory.db", projection="pca")
        tsne = G.build_graph(conn, tmp_path / "memory.db", projection="tsne")
        assert tsne["meta"]["cached"] is False
        assert [n["x"] for n in pca["nodes"]] != [n["x"] for n in tsne["nodes"]]

    def test_layout_version_invalidates_stale_coordinates(
        self, clustered_db, tmp_path, monkeypatch
    ) -> None:
        """Changing the layout maths must not keep serving old coordinates.

        The key is otherwise pure corpus membership, so without the version a
        code change would silently reuse the previous algorithm's output.
        """
        conn, _ = clustered_db
        G.build_graph(conn, tmp_path / "memory.db", projection="pca")
        G._cache = None
        monkeypatch.setattr(G, "_LAYOUT_VERSION", G._LAYOUT_VERSION + 1)
        result = G.build_graph(conn, tmp_path / "memory.db", projection="pca")
        assert result["meta"]["cached"] is False

    def test_unwritable_cache_directory_is_not_fatal(
        self, clustered_db, tmp_path, monkeypatch
    ) -> None:
        """A read-only volume must degrade to in-memory, not break the view."""
        conn, _ = clustered_db

        def _boom(*args, **kwargs):
            raise OSError("read-only file system")

        monkeypatch.setattr("pathlib.Path.mkdir", _boom)
        assert G.build_graph(conn, tmp_path / "memory.db", projection="pca")["nodes"]


# ---------------------------------------------------------------------------
# build_graph contract
# ---------------------------------------------------------------------------


class TestBuildGraph:
    def test_reading_the_graph_never_writes(self, clustered_db, tmp_path) -> None:
        """The view must not perturb the corpus it observes.

        ``access_count`` feeds the frequency term of the retrieval scorer, so a
        graph that counted as a retrieval would distort the very ranking it
        exists to expose — silently, and more the more you looked at it.
        """
        conn, _ = clustered_db
        query = "SELECT sum(access_count), sum(length(content)), count(*) FROM memories"
        before = tuple(conn.execute(query).fetchone())
        for mode in G.EDGE_MODES:
            G.build_graph(conn, tmp_path / "memory.db", projection="pca", edges=mode)
        assert tuple(conn.execute(query).fetchone()) == before

    def test_covers_the_whole_corpus_including_archived(self, clustered_db, tmp_path) -> None:
        """Filtering is the client's job, so positions stay stable across views.

        If the server dropped archived rows the layout would shift whenever a
        filter changed, which is what makes a spatial view unlearnable.
        """
        conn, vectors = clustered_db
        _insert(conn, "mem-archived", vectors[0].tolist(), tier="archived")
        result = G.build_graph(conn, tmp_path / "memory.db", projection="pca")
        assert "mem-archived" in {n["id"] for n in result["nodes"]}

    def test_node_payload_carries_a_preview_not_full_content(
        self, clustered_db, tmp_path
    ) -> None:
        """Full content averages 2.7 KB/memory — far too much to ship per node."""
        conn, _ = clustered_db
        conn.execute("UPDATE memories SET content = ? WHERE id = 'mem-0000'", ("x" * 10_000,))
        node = next(
            n
            for n in G.build_graph(conn, tmp_path / "memory.db", projection="pca")["nodes"]
            if n["id"] == "mem-0000"
        )
        assert len(node["preview"]) <= G._PREVIEW_CHARS + 1
        assert "content" not in node

    def test_edge_indices_address_returned_nodes(self, clustered_db, tmp_path) -> None:
        conn, _ = clustered_db
        result = G.build_graph(
            conn, tmp_path / "memory.db", projection="pca", edges="semantic"
        )
        count = len(result["nodes"])
        assert all(0 <= i < count and 0 <= j < count for i, j, _ in result["edges"])

    def test_unknown_edge_mode_is_rejected(self, clustered_db, tmp_path) -> None:
        conn, _ = clustered_db
        with pytest.raises(G.GraphError):
            G.build_graph(conn, tmp_path / "memory.db", edges="nonsense")

    def test_projection_is_validated_before_it_reaches_the_cache_path(
        self, clustered_db, tmp_path
    ) -> None:
        """The projection name becomes a path segment in the cache key.

        Validating it only where it is first *used* -- inside _project(), well
        after the key is built -- would let an arbitrary caller-supplied string
        reach a filesystem lookup before anything checked it.
        """
        conn, _ = clustered_db
        with pytest.raises(G.GraphError, match="Unknown projection"):
            G.build_graph(
                conn, tmp_path / "memory.db", projection="../../../etc/passwd"
            )
        assert not (tmp_path / "_graph_cache").exists()

    def test_empty_corpus_returns_an_empty_graph(self, db_conn, tmp_path) -> None:
        result = G.build_graph(db_conn, tmp_path / "memory.db")
        assert result["nodes"] == [] and result["edges"] == []
        assert result["meta"]["node_count"] == 0

    def test_memories_without_embeddings_are_dropped_not_zero_filled(
        self, clustered_db, tmp_path
    ) -> None:
        """A zero vector would sit at the origin and read as a real cluster."""
        conn, _ = clustered_db
        conn.execute("DELETE FROM memories_vec WHERE id = 'mem-0000'")
        result = G.build_graph(conn, tmp_path / "memory.db", projection="pca")
        assert "mem-0000" not in {n["id"] for n in result["nodes"]}

    def test_truncation_is_reported_never_silent(
        self, clustered_db, tmp_path, monkeypatch
    ) -> None:
        conn, _ = clustered_db
        monkeypatch.setattr(G, "_MAX_EDGES", 5)
        # threshold=0 so the cap, not the fixture's similarity range, is what
        # limits the edge count.
        meta = G.build_graph(
            conn, tmp_path / "memory.db", projection="pca", edges="semantic", threshold=0.0
        )["meta"]
        assert meta["truncated"] is True
        assert meta["edge_count"] == 5 < meta["total_edges"]

    def test_truncation_keeps_the_strongest_edges(
        self, clustered_db, tmp_path, monkeypatch
    ) -> None:
        """Dropping the weakest links is the only defensible way to truncate."""
        conn, _ = clustered_db
        full = G.build_graph(
            conn, tmp_path / "memory.db", projection="pca", edges="semantic", threshold=0.0
        )
        assert len(full["edges"]) > 5, "need more edges than the cap to test capping"
        strongest = sorted((e[2] for e in full["edges"]), reverse=True)[:5]

        monkeypatch.setattr(G, "_MAX_EDGES", 5)
        capped = G.build_graph(
            conn, tmp_path / "memory.db", projection="pca", edges="semantic", threshold=0.0
        )
        assert sorted((e[2] for e in capped["edges"]), reverse=True) == strongest


# ---------------------------------------------------------------------------
# Detail endpoint
# ---------------------------------------------------------------------------


class TestMemoryDetail:
    async def test_returns_full_content_without_counting_a_retrieval(
        self, db_conn, monkeypatch
    ) -> None:
        """Inspecting a memory is not retrieving it.

        Mirrors the guarantee ``memory_why`` already makes; without it, browsing
        the graph would inflate the access count of everything clicked.
        """
        from claude_memory.mcp import tools

        _insert(db_conn, "mem-x", [0.1] * 384)
        db_conn.execute(
            "UPDATE memories SET content = ?, access_count = 7 WHERE id = 'mem-x'",
            ("full body",),
        )
        monkeypatch.setattr(tools, "_get_deps", lambda: (_NoCloseConn(db_conn), None, None))

        result = await tools.tool_memory_get(memory_id="mem-x")

        assert result["found"] is True
        assert result["content"] == "full body"
        after = db_conn.execute(
            "SELECT access_count FROM memories WHERE id='mem-x'"
        ).fetchone()[0]
        assert after == 7, "inspecting a memory must not count as a retrieval"

    async def test_unknown_id_reports_not_found(self, db_conn, monkeypatch) -> None:
        from claude_memory.mcp import tools

        monkeypatch.setattr(tools, "_get_deps", lambda: (_NoCloseConn(db_conn), None, None))
        assert (await tools.tool_memory_get(memory_id="nope"))["found"] is False
