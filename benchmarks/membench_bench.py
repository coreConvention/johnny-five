#!/usr/bin/env python3
"""johnny-five × MemBench retrieval benchmark (ACL 2025).

Standalone driver. Same pattern as ``locomo_bench.py`` and ``convomem_bench.py``
— no mempalace imports. Uses ``claude_memory.retrieval.search.search_memories``
against an in-memory SQLite DB built per item.

What this measures
------------------
MemBench (ACL 2025, https://aclanthology.org/2025.findings-acl.989/) has
~8500 QA items across 11 categories. Each item has a multi-turn
conversation and a QA with a ``target_step_id`` pointing at the turn(s)
that contain the answer. Retrieval recall = hit if any target sid/global_idx
appears in top-K retrieved.

Mempalace published 80.3% R@5 across all categories (movie + roles + events).

Run
---
    git clone https://github.com/import-myself/Membench.git /tmp/membench

    # Full run (default: topic=movie, top-k=5, kappa=0.30, all categories)
    python benchmarks/membench_bench.py /tmp/membench/MemData/FirstAgent

    # Subset for quick sanity
    python benchmarks/membench_bench.py /tmp/membench/MemData/FirstAgent \\
        --category highlevel --limit 50

    # Raw baseline for delta
    python benchmarks/membench_bench.py /tmp/membench/MemData/FirstAgent --kappa 0
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from claude_memory.db.connection import get_connection
from claude_memory.db.queries import MemoryRecord, insert_memory
from claude_memory.embeddings.encoder import get_encoder
from claude_memory.retrieval.scorer import ScoringWeights
from claude_memory.retrieval.search import search_memories


CATEGORY_FILES = {
    "simple": "simple.json",
    "highlevel": "highlevel.json",
    "knowledge_update": "knowledge_update.json",
    "comparative": "comparative.json",
    "conditional": "conditional.json",
    "noisy": "noisy.json",
    "aggregative": "aggregative.json",
    "highlevel_rec": "highlevel_rec.json",
    "lowlevel_rec": "lowlevel_rec.json",
    "RecMultiSession": "RecMultiSession.json",
    "post_processing": "post_processing.json",
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_membench(
    data_dir: str,
    categories: list[str] | None = None,
    topic: str = "movie",
    limit: int = 0,
) -> list[dict]:
    """Load items from FirstAgent directory. Filters by topic when applicable."""
    data_path = Path(data_dir)
    if categories is None:
        categories = list(CATEGORY_FILES.keys())

    items: list[dict] = []
    for cat in categories:
        fname = CATEGORY_FILES.get(cat)
        if not fname:
            continue
        fp = data_path / fname
        if not fp.exists():
            continue
        with open(fp, encoding="utf-8") as f:
            raw = json.load(f)

        # Topic-keyed: {movie: [...], food: [...], book: [...]}
        # Role-keyed: {roles: [...], events: [...]}
        for t, topic_items in raw.items():
            if topic and t not in (topic, "roles", "events"):
                continue
            if not isinstance(topic_items, list):
                continue
            for item in topic_items:
                turns = item.get("message_list", [])
                qa = item.get("QA", {})
                if not turns or not qa:
                    continue
                items.append({
                    "category": cat,
                    "topic": t,
                    "tid": item.get("tid", 0),
                    "turns": turns,
                    "question": qa.get("question", ""),
                    "ground_truth": qa.get("ground_truth", ""),
                    "target_step_ids": qa.get("target_step_id", []),
                })
    if limit > 0:
        items = items[:limit]
    return items


def _turn_text(turn: dict) -> str:
    user = turn.get("user") or turn.get("user_message", "")
    asst = turn.get("assistant") or turn.get("assistant_message", "")
    t = turn.get("time", "")
    text = f"[User] {user} [Assistant] {asst}"
    if t:
        text = f"[{t}] " + text
    return text


def _flatten_turns(message_list) -> list[dict]:
    """Normalise message_list into a flat list of turn dicts.

    MemBench files have two shapes: a flat list of turns, or a list of
    sessions each containing turns. We flatten to a single sequence and
    return the turns tagged with their sid/global_idx.
    """
    if not message_list:
        return []
    # If the first element is a dict, it's a flat list of turns
    # (highlevel.json); else it's nested sessions (simple.json).
    if isinstance(message_list[0], dict):
        sessions = [message_list]
    else:
        sessions = message_list

    out: list[dict] = []
    g = 0
    for s_idx, sess in enumerate(sessions):
        if not isinstance(sess, list):
            continue
        for t_idx, turn in enumerate(sess):
            if not isinstance(turn, dict):
                continue
            sid = turn.get("sid", turn.get("mid", g))
            try:
                sid_int = int(sid)
            except Exception:
                sid_int = g
            out.append({
                "global_idx": g,
                "sid": sid_int,
                "s_idx": s_idx,
                "t_idx": t_idx,
                "text": _turn_text(turn),
            })
            g += 1
    return out


# ---------------------------------------------------------------------------
# Retrieval per item
# ---------------------------------------------------------------------------

def _hit_for_item(
    item: dict,
    encoder,
    weights: ScoringWeights,
    top_k: int,
) -> tuple[bool, dict]:
    """Build fresh DB, query, return (hit, details)."""
    turns = _flatten_turns(item["turns"])
    if not turns:
        return False, {"error": "empty turns"}

    question = item["question"] or ""
    # target_step_id can be [sid, ?] lists — collect the first element of each
    target_sids: set[int] = set()
    for step in item.get("target_step_ids", []):
        if isinstance(step, list) and len(step) >= 1:
            v = step[0]
            if isinstance(v, (int, float)):
                target_sids.add(int(v))

    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="j5_membench_")
    os.close(fd)

    try:
        conn = get_connection(Path(db_path), embedding_dim=384)
        for turn in turns:
            emb = encoder.encode(turn["text"])
            rec = MemoryRecord(
                id=f"t_{turn['global_idx']}",
                content=turn["text"],
                summary=None,
                type="project",
                tags=[],
                created_at="2024-01-01T00:00:00+00:00",
                updated_at="2024-01-01T00:00:00+00:00",
                last_accessed="2024-01-01T00:00:00+00:00",
                access_count=0,
                importance=5.0,
                tier="hot",
                project_dir="membench_item",
                source_session=None,
                supersedes=None,
                consolidated_from=[],
                metadata={
                    "sid": turn["sid"],
                    "global_idx": turn["global_idx"],
                },
            )
            insert_memory(conn, rec, emb)
        conn.commit()

        results = search_memories(
            conn=conn,
            encoder=encoder,
            query=question,
            project_dir="membench_item",
            weights=weights,
            top_k=min(top_k, len(turns)),
            update_access_on_retrieve=False,
        )

        retrieved_sids: set[int] = set()
        retrieved_globals: set[int] = set()
        for r in results:
            meta = r.memory.metadata
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            sid = meta.get("sid")
            g = meta.get("global_idx")
            if isinstance(sid, int):
                retrieved_sids.add(sid)
            if isinstance(g, int):
                retrieved_globals.add(g)

        hit = bool(target_sids & retrieved_sids) or bool(target_sids & retrieved_globals)
        conn.close()
        return hit, {
            "target_sids": sorted(target_sids),
            "retrieved_sids": sorted(retrieved_sids),
            "retrieved_globals": sorted(retrieved_globals),
        }
    finally:
        Path(db_path).unlink(missing_ok=True)
        for sfx in ("-wal", "-shm"):
            Path(db_path + sfx).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_benchmark(
    data_dir: str,
    categories: list[str] | None,
    topic: str,
    top_k: int,
    limit: int,
    kappa: float,
    out_file: str | None,
) -> dict:
    items = load_membench(data_dir, categories=categories, topic=topic, limit=limit)
    if not items:
        print(f"No items found in {data_dir}")
        return {"error": "no items"}

    weights = ScoringWeights(alpha=1.0, beta=0.0, gamma=0.0, delta=0.0, kappa=kappa)
    encoder = get_encoder("all-MiniLM-L6-v2")
    encoder.encode("warmup")

    cats_label = ", ".join(categories) if categories else "all"
    print(f"\n{'=' * 60}")
    print("  johnny-five x MemBench Benchmark")
    print(f"{'=' * 60}")
    print(f"  Data:        {data_dir}")
    print(f"  Categories:  {cats_label}")
    print(f"  Topic:       {topic or 'all'}")
    print(f"  Items:       {len(items)}")
    print(f"  Top-k:       {top_k}")
    print(f"  kappa:       {kappa}")
    print(f"{'-' * 60}\n")

    by_cat: dict[str, dict[str, int]] = defaultdict(lambda: {"hit": 0, "total": 0})
    total_hit = 0
    results_log: list[dict] = []
    t0 = time.time()

    for idx, item in enumerate(items, 1):
        hit, details = _hit_for_item(item, encoder, weights, top_k=top_k)
        cat = item["category"]
        by_cat[cat]["total"] += 1
        if hit:
            total_hit += 1
            by_cat[cat]["hit"] += 1
        results_log.append({
            "category": cat,
            "topic": item["topic"],
            "tid": item["tid"],
            "question": item["question"],
            "hit": hit,
            "details": details,
        })
        if idx % 50 == 0 or idx == len(items):
            running = total_hit / idx * 100
            print(f"  [{idx:4}/{len(items)}] running R@{top_k}: {running:.1f}%  elapsed={time.time() - t0:.0f}s")

    elapsed = time.time() - t0
    overall = total_hit / len(items) * 100 if items else 0.0

    print(f"\n{'=' * 60}")
    print(f"  RESULTS - johnny-five MemBench (kappa={kappa}, top-{top_k})")
    print(f"{'=' * 60}")
    print(f"  Time:        {elapsed:.1f}s  ({elapsed / max(len(items), 1):.2f}s/item)")
    print(f"  Overall R@{top_k}: {overall:.1f}%  ({total_hit}/{len(items)})")
    print()
    print("  By category:")
    for cat in sorted(by_cat):
        v = by_cat[cat]
        pct = v["hit"] / v["total"] * 100 if v["total"] else 0.0
        print(f"    {cat:18} {pct:5.1f}%  ({v['hit']}/{v['total']})")
    print(f"{'=' * 60}\n")

    summary = {
        "kappa": kappa,
        "top_k": top_k,
        "topic": topic,
        "total_items": len(items),
        "overall_pct": overall,
        "by_category": {
            cat: {
                "hit": v["hit"],
                "total": v["total"],
                "pct": (v["hit"] / v["total"] * 100) if v["total"] else 0.0,
            }
            for cat, v in by_cat.items()
        },
        "elapsed_sec": elapsed,
    }
    if out_file:
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print(f"  Summary written to {out_file}")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="johnny-five x MemBench")
    parser.add_argument("data_dir", help="Path to MemBench FirstAgent directory")
    parser.add_argument(
        "--category",
        default=None,
        choices=list(CATEGORY_FILES.keys()),
        help="Run a single category (default: all)",
    )
    parser.add_argument("--topic", default="movie")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--kappa", type=float, default=0.30)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    cats = [args.category] if args.category else None
    run_benchmark(
        data_dir=args.data_dir,
        categories=cats,
        topic=args.topic,
        top_k=args.top_k,
        limit=args.limit,
        kappa=args.kappa,
        out_file=args.out,
    )
