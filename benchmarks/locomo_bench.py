#!/usr/bin/env python3
"""johnny-five × LoCoMo retrieval benchmark.

Standalone driver — does not import mempalace's benchmark code. Uses
johnny-five's internals directly (`search_memories`) against an in-memory
SQLite database rebuilt per conversation. No docker, no MCP overhead.

What this measures
------------------
LoCoMo (Long Conversation Memory) from snap-research: 10 multi-session
personal conversations, ~200 QA pairs across 5 categories (single-hop,
temporal, temporal-inference, open-domain, adversarial). For each QA we
check whether the evidence session is in the top-K retrieved results.

Comparison context
------------------
Mempalace published numbers on the same dataset, same top-k=10, same
session granularity:

    raw (semantic-only, no boost):     R@10 = 60.3%
    hybrid v5 (mult. keyword+name):    R@10 = 88.9%

Johnny-five's kappa boost is additive on a [0,1] combined score (not
multiplicative on distance like mempalace), so we don't expect to match
88.9% exactly — but a meaningful lift from MEMORY_KAPPA=0 → 0.30 is the
test.

Run
---
    # Get the dataset:
    git clone https://github.com/snap-research/locomo.git /tmp/locomo

    # Install johnny-five in editable mode:
    pip install -e ".[dev]"

    # Run with default kappa=0.30 (hybrid):
    python benchmarks/locomo_bench.py /tmp/locomo/data/locomo10.json

    # Run raw (no keyword boost) for the before/after delta:
    python benchmarks/locomo_bench.py /tmp/locomo/data/locomo10.json --kappa 0

    # Smoke test on a single conversation:
    python benchmarks/locomo_bench.py /tmp/locomo/data/locomo10.json --limit 1

Note on scoring weights: we zero β (recency), γ (frequency), and δ
(importance) for this benchmark because every ingested session is
brand-new, never accessed, and has identical importance — those signals
carry no information here. Only α (semantic) and kappa (lexical) vary across
candidates, which is exactly what we want to measure.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from claude_memory.db.connection import get_connection
from claude_memory.db.queries import MemoryRecord, insert_memory
from claude_memory.embeddings.encoder import get_encoder
from claude_memory.retrieval.scorer import ScoringWeights
from claude_memory.retrieval.search import search_memories


CATEGORIES = {
    1: "Single-hop",
    2: "Temporal",
    3: "Temporal-inference",
    4: "Open-domain",
    5: "Adversarial",
}


# ---------------------------------------------------------------------------
# LoCoMo data loading (mirrors the snap-research format)
# ---------------------------------------------------------------------------

def load_conversation_sessions(conversation: dict) -> list[dict]:
    """Extract ``session_N`` dicts from a LoCoMo conversation."""
    sessions: list[dict] = []
    n = 1
    while True:
        key = f"session_{n}"
        if key not in conversation:
            break
        sessions.append({
            "session_num": n,
            "date": conversation.get(f"session_{n}_date_time", ""),
            "dialogs": conversation[key],
        })
        n += 1
    return sessions


def build_corpus(
    sessions: list[dict], granularity: str = "session",
) -> tuple[list[str], list[str], list[str]]:
    """Turn sessions into parallel corpus/ids/timestamps lists.

    'session' granularity joins all dialogs of a session into one doc;
    'dialog' granularity emits one doc per dialog turn.
    """
    corpus: list[str] = []
    corpus_ids: list[str] = []
    corpus_timestamps: list[str] = []

    for sess in sessions:
        if granularity == "session":
            texts = [
                f'{d.get("speaker", "?")} said, "{d.get("text", "")}"'
                for d in sess["dialogs"]
            ]
            corpus.append("\n".join(texts))
            corpus_ids.append(f"session_{sess['session_num']}")
            corpus_timestamps.append(sess["date"])
        else:  # dialog
            for d in sess["dialogs"]:
                dia_id = d.get("dia_id", f"D{sess['session_num']}:?")
                corpus.append(
                    f'{d.get("speaker", "?")} said, "{d.get("text", "")}"'
                )
                corpus_ids.append(dia_id)
                corpus_timestamps.append(sess["date"])

    return corpus, corpus_ids, corpus_timestamps


# ---------------------------------------------------------------------------
# Evidence → id mapping + recall
# ---------------------------------------------------------------------------

def evidence_to_session_ids(evidence: list[str]) -> set[str]:
    """Turn evidence like ``['D3:5', 'D3:7']`` into ``{'session_3'}``."""
    result: set[str] = set()
    for eid in evidence:
        m = re.match(r"D(\d+):", eid)
        if m:
            result.add(f"session_{m.group(1)}")
    return result


def compute_recall(retrieved_ids: list[str], evidence_ids: set[str]) -> float:
    """Fraction of evidence ids present in retrieved results."""
    if not evidence_ids:
        return 1.0
    hits = sum(1 for eid in evidence_ids if eid in retrieved_ids)
    return hits / len(evidence_ids)


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def run_benchmark(
    data_file: str,
    top_k: int = 10,
    kappa: float = 0.30,
    granularity: str = "session",
    limit: int = 0,
) -> dict:
    with open(data_file) as f:
        data = json.load(f)
    if limit > 0:
        data = data[:limit]

    # Only α (semantic) and kappa (lexical) are informative signals for this
    # benchmark; see module docstring.
    weights = ScoringWeights(alpha=1.0, beta=0.0, gamma=0.0, delta=0.0, kappa=kappa)

    encoder = get_encoder("all-MiniLM-L6-v2")
    # Warm up the encoder once so the first per-conversation loop isn't skewed.
    encoder.encode("warmup")
    print(f"[{time.strftime('%H:%M:%S')}] encoder ready — running {len(data)} conversations")

    all_recall: list[float] = []
    per_category: dict[int, list[float]] = defaultdict(list)
    total_qa = 0
    t0 = time.time()

    for conv_idx, sample in enumerate(data):
        sample_id = sample.get("sample_id", f"conv-{conv_idx}")
        conversation = sample["conversation"]
        qa_pairs = sample["qa"]
        sessions = load_conversation_sessions(conversation)
        corpus, corpus_ids, corpus_timestamps = build_corpus(sessions, granularity)

        # Fresh in-memory-ish SQLite per conversation.
        # WAL mode requires a real file, so we use a named temp file and
        # clean it up after.
        fd, db_path = tempfile.mkstemp(suffix=".db", prefix="j5_locomo_")
        import os
        os.close(fd)

        try:
            conn = get_connection(Path(db_path), embedding_dim=384)

            # Ingest corpus — one insert_memory per doc
            for i, (doc, cid, ts) in enumerate(
                zip(corpus, corpus_ids, corpus_timestamps)
            ):
                embedding = encoder.encode(doc)
                record = MemoryRecord(
                    id=f"{sample_id}:{cid}:{i}",
                    content=doc,
                    summary=None,
                    type="project",
                    tags=[cid],  # store corpus_id as a tag for keyword-boost indexing
                    created_at=ts or "2024-01-01T00:00:00+00:00",
                    updated_at=ts or "2024-01-01T00:00:00+00:00",
                    last_accessed=ts or "2024-01-01T00:00:00+00:00",
                    access_count=0,
                    importance=5.0,
                    tier="hot",
                    project_dir=sample_id,
                    source_session=None,
                    supersedes=None,
                    consolidated_from=[],
                    metadata={"corpus_id": cid},
                )
                insert_memory(conn, record, embedding)
            conn.commit()

            # Query each QA pair
            for qa in qa_pairs:
                question = qa["question"]
                category = qa["category"]
                evidence = qa.get("evidence", [])

                results = search_memories(
                    conn=conn,
                    encoder=encoder,
                    query=question,
                    project_dir=sample_id,
                    weights=weights,
                    top_k=top_k,
                    update_access_on_retrieve=False,
                )

                # Extract corpus_id from metadata JSON (search results carry
                # MemoryRecord which has metadata as a dict already).
                retrieved_cids: list[str] = []
                for r in results:
                    meta = r.memory.metadata
                    if isinstance(meta, str):
                        try:
                            meta = json.loads(meta)
                        except Exception:
                            meta = {}
                    retrieved_cids.append(meta.get("corpus_id", r.memory.id))

                if granularity == "session":
                    evidence_set = evidence_to_session_ids(evidence)
                else:
                    evidence_set = set(evidence)

                recall = compute_recall(retrieved_cids, evidence_set)
                all_recall.append(recall)
                per_category[category].append(recall)
                total_qa += 1

            conn.close()
        finally:
            Path(db_path).unlink(missing_ok=True)
            # WAL sidecars
            for suffix in ("-wal", "-shm"):
                Path(db_path + suffix).unlink(missing_ok=True)

        elapsed_so_far = time.time() - t0
        print(
            f"  [{conv_idx + 1}/{len(data)}] {sample_id}: "
            f"{len(sessions)} sessions, {len(qa_pairs)} Qs "
            f"(total elapsed {elapsed_so_far:.1f}s)"
        )

    elapsed = time.time() - t0
    avg_recall = sum(all_recall) / len(all_recall) if all_recall else 0.0

    print(f"\n{'=' * 60}")
    print(f"  johnny-five x LoCoMo  (kappa={kappa}, top-{top_k}, {granularity})")
    print(f"{'=' * 60}")
    print(f"  Time:        {elapsed:.1f}s  ({elapsed / max(total_qa, 1):.2f}s/q)")
    print(f"  Questions:   {total_qa}")
    print(f"  Avg Recall:  {avg_recall:.4f}")
    print()
    print("  PER-CATEGORY RECALL:")
    for cat in sorted(per_category):
        vals = per_category[cat]
        name = CATEGORIES.get(cat, f"Cat-{cat}")
        print(f"    {name:25} R={sum(vals) / len(vals):.3f}  (n={len(vals)})")

    perfect = sum(1 for r in all_recall if r >= 1.0)
    partial = sum(1 for r in all_recall if 0 < r < 1.0)
    zero = sum(1 for r in all_recall if r == 0)
    print()
    print("  RECALL DISTRIBUTION:")
    denom = max(len(all_recall), 1)
    print(f"    Perfect (1.0):  {perfect:4}  ({100 * perfect / denom:.1f}%)")
    print(f"    Partial (0-1):  {partial:4}  ({100 * partial / denom:.1f}%)")
    print(f"    Zero (0.0):     {zero:4}  ({100 * zero / denom:.1f}%)")
    print(f"{'=' * 60}\n")

    return {
        "kappa": kappa,
        "top_k": top_k,
        "granularity": granularity,
        "total_qa": total_qa,
        "avg_recall": avg_recall,
        "per_category": {
            CATEGORIES.get(k, str(k)): sum(v) / len(v) for k, v in per_category.items()
        },
        "elapsed_sec": elapsed,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="johnny-five × LoCoMo retrieval benchmark",
    )
    parser.add_argument("data_file", help="Path to locomo10.json")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--kappa",
        type=float,
        default=0.30,
        help="Lexical-overlap weight (0 = semantic-only baseline, 0.30 = default hybrid)",
    )
    parser.add_argument(
        "--granularity",
        choices=["session", "dialog"],
        default="session",
    )
    parser.add_argument("--limit", type=int, default=0, help="Limit to N conversations")
    parser.add_argument("--out", default=None, help="Write JSON summary to this path")
    args = parser.parse_args()

    result = run_benchmark(
        args.data_file,
        top_k=args.top_k,
        kappa=args.kappa,
        granularity=args.granularity,
        limit=args.limit,
    )

    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"  Summary written to {args.out}")
