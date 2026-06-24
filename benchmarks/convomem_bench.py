#!/usr/bin/env python3
"""johnny-five × ConvoMem retrieval benchmark.

Standalone driver. Mirrors the structure of ``locomo_bench.py`` — no
mempalace imports, uses ``claude_memory.retrieval.search.search_memories``
in-process against an in-memory SQLite DB built per item.

What this measures
------------------
ConvoMem (Salesforce, 75K+ QA pairs) evidence-question recall over 6
categories. Each item has: a set of conversations (messages with speaker),
a question, and ``message_evidences`` (the target messages). Recall
per item = fraction of evidence texts found (via substring match) within
the top-k retrieved messages.

Mempalace published: 92.9% avg recall with 50 items per category across
all 6 categories (their raw verbatim + default embeddings pipeline).

Run
---
    # Default: 50 items per category × 6 categories = 300 items
    python benchmarks/convomem_bench.py

    # Subset:
    python benchmarks/convomem_bench.py --limit 20 --category user_evidence

    # Raw baseline for delta comparison:
    python benchmarks/convomem_bench.py --kappa 0

Downloads each item's JSON from HuggingFace the first time and caches
to ``/tmp/convomem-cache/`` so reruns are offline.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

from claude_memory.db.connection import get_connection
from claude_memory.db.queries import MemoryRecord, insert_memory
from claude_memory.embeddings.encoder import get_encoder
from claude_memory.retrieval.scorer import ScoringWeights
from claude_memory.retrieval.search import search_memories


HF_BASE = (
    "https://huggingface.co/datasets/Salesforce/ConvoMem/resolve/main/"
    "core_benchmark/evidence_questions"
)
HF_API = (
    "https://huggingface.co/api/datasets/Salesforce/ConvoMem/tree/main/"
    "core_benchmark/evidence_questions"
)

CATEGORIES = {
    "user_evidence": "User Facts",
    "assistant_facts_evidence": "Assistant Facts",
    "changing_evidence": "Changing Facts",
    "abstention_evidence": "Abstention",
    "preference_evidence": "Preferences",
    "implicit_connection_evidence": "Implicit Connections",
}


# ---------------------------------------------------------------------------
# Data loading — HuggingFace download + on-disk cache
# ---------------------------------------------------------------------------

def _cache_path(cache_dir: str, category: str, subpath: str) -> str:
    safe = subpath.replace("/", "_")
    p = os.path.join(cache_dir, category, safe)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def download_evidence_file(category: str, subpath: str, cache_dir: str) -> dict | None:
    """Download one evidence JSON. Returns parsed dict, or None on failure."""
    cache = _cache_path(cache_dir, category, subpath)
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            return json.load(f)

    url = f"{HF_BASE}/{category}/{subpath}"
    try:
        urllib.request.urlretrieve(url, cache)
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"    download failed for {category}/{subpath}: {e}")
        return None


def discover_files(category: str, cache_dir: str) -> list[str]:
    """List JSON files under ``<category>/1_evidence/`` via the HF API."""
    cache = os.path.join(cache_dir, f"{category}_filelist.json")
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            return json.load(f)

    api_url = f"{HF_API}/{category}/1_evidence"
    try:
        with urllib.request.urlopen(urllib.request.Request(api_url), timeout=20) as resp:
            files = json.loads(resp.read())
            paths = [
                f["path"].split(f"{category}/")[1]
                for f in files
                if f.get("path", "").endswith(".json")
            ]
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(paths, f)
            return paths
    except Exception as e:
        print(f"    file-list failed for {category}: {e}")
        return []


def load_evidence_items(
    categories: list[str], limit_per_cat: int, cache_dir: str,
) -> list[dict]:
    """Load up to ``limit_per_cat`` items for each category."""
    items: list[dict] = []
    for category in categories:
        files = discover_files(category, cache_dir)
        if not files:
            print(f"  {category}: no files found")
            continue
        for_cat: list[dict] = []
        for fp in files:
            if len(for_cat) >= limit_per_cat:
                break
            data = download_evidence_file(category, fp, cache_dir)
            if data and "evidence_items" in data:
                for ev in data["evidence_items"]:
                    ev["_category_key"] = category
                    for_cat.append(ev)
                    if len(for_cat) >= limit_per_cat:
                        break
        items.extend(for_cat[:limit_per_cat])
        print(
            f"  {CATEGORIES.get(category, category):25} "
            f"{len(for_cat[:limit_per_cat])} items loaded"
        )
    return items


# ---------------------------------------------------------------------------
# Retrieval + scoring per item
# ---------------------------------------------------------------------------

def retrieve_for_item(
    item: dict,
    encoder,
    weights: ScoringWeights,
    top_k: int,
) -> tuple[float, dict]:
    """Build a fresh johnny-five DB for this item; query; compute recall."""
    conversations = item.get("conversations", []) or []
    question: str = item["question"]
    evidences = item.get("message_evidences", []) or []
    evidence_texts: set[str] = {e["text"].strip().lower() for e in evidences if e.get("text")}

    # Flatten messages into a corpus.
    corpus: list[str] = []
    speakers: list[str] = []
    for conv in conversations:
        for msg in conv.get("messages", []) or []:
            corpus.append(msg.get("text", ""))
            speakers.append(msg.get("speaker", "?"))

    if not corpus:
        return 0.0, {"error": "empty corpus"}

    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="j5_convomem_")
    os.close(fd)

    try:
        conn = get_connection(Path(db_path), embedding_dim=384)
        # Ingest
        for i, (doc, speaker) in enumerate(zip(corpus, speakers)):
            if not doc:
                continue
            embedding = encoder.encode(doc)
            record = MemoryRecord(
                id=f"msg_{i}",
                content=doc,
                summary=None,
                type="project",
                tags=[speaker] if speaker else [],
                created_at="2024-01-01T00:00:00+00:00",
                updated_at="2024-01-01T00:00:00+00:00",
                last_accessed="2024-01-01T00:00:00+00:00",
                access_count=0,
                importance=5.0,
                tier="hot",
                project_dir="convomem_item",
                source_session=None,
                supersedes=None,
                consolidated_from=[],
                metadata={"idx": i, "speaker": speaker},
            )
            insert_memory(conn, record, embedding)
        conn.commit()

        # Retrieve
        results = search_memories(
            conn=conn,
            encoder=encoder,
            query=question,
            project_dir="convomem_item",
            weights=weights,
            top_k=min(top_k, len(corpus)),
            update_access_on_retrieve=False,
        )
        retrieved_texts = [r.memory.content.strip().lower() for r in results]

        # Substring match either direction (mempalace's exact methodology).
        found = 0
        for ev_text in evidence_texts:
            for ret_text in retrieved_texts:
                if ev_text in ret_text or ret_text in ev_text:
                    found += 1
                    break

        recall = (found / len(evidence_texts)) if evidence_texts else 1.0

        conn.close()
        return recall, {
            "retrieved_count": len(retrieved_texts),
            "evidence_count": len(evidence_texts),
            "found": found,
        }
    finally:
        Path(db_path).unlink(missing_ok=True)
        for sfx in ("-wal", "-shm"):
            Path(db_path + sfx).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_benchmark(
    categories: list[str],
    limit_per_cat: int,
    top_k: int,
    kappa: float,
    cache_dir: str,
    out_file: str | None,
) -> dict:
    weights = ScoringWeights(alpha=1.0, beta=0.0, gamma=0.0, delta=0.0, kappa=kappa)

    encoder = get_encoder("all-MiniLM-L6-v2")
    encoder.encode("warmup")

    print(f"\n{'=' * 60}")
    print("  johnny-five x ConvoMem Benchmark")
    print(f"{'=' * 60}")
    print(f"  Categories:  {len(categories)}")
    print(f"  Limit/cat:   {limit_per_cat}")
    print(f"  Top-k:       {top_k}")
    print(f"  kappa:       {kappa}")
    print(f"  Cache dir:   {cache_dir}")
    print(f"{'-' * 60}\n  Loading data from HuggingFace (cached after first run)...\n")

    items = load_evidence_items(categories, limit_per_cat, cache_dir)
    print(f"\n  Total items: {len(items)}\n{'-' * 60}\n")

    if not items:
        print("  No items loaded — aborting.")
        return {"error": "no items"}

    all_recall: list[float] = []
    per_category: dict[str, list[float]] = defaultdict(list)
    results_log: list[dict] = []
    t0 = time.time()

    for i, item in enumerate(items):
        cat_key = item.get("_category_key", "unknown")
        recall, details = retrieve_for_item(item, encoder, weights, top_k=top_k)
        all_recall.append(recall)
        per_category[cat_key].append(recall)
        results_log.append({
            "question": item.get("question", ""),
            "category": cat_key,
            "recall": recall,
            "details": details,
        })

        if (i + 1) % 25 == 0 or i == len(items) - 1:
            avg = sum(all_recall) / len(all_recall)
            print(
                f"  [{i + 1:4}/{len(items)}] "
                f"avg_recall={avg:.3f}  elapsed={time.time() - t0:.0f}s"
            )

    elapsed = time.time() - t0
    avg_recall = sum(all_recall) / len(all_recall) if all_recall else 0.0

    print(f"\n{'=' * 60}")
    print(f"  RESULTS — johnny-five ConvoMem (kappa={kappa}, top-{top_k})")
    print(f"{'=' * 60}")
    print(f"  Time:        {elapsed:.1f}s  ({elapsed / max(len(items), 1):.2f}s/item)")
    print(f"  Items:       {len(items)}")
    print(f"  Avg Recall:  {avg_recall:.4f}")
    print()
    print("  PER-CATEGORY RECALL:")
    for cat_key in sorted(per_category):
        vals = per_category[cat_key]
        perfect = sum(1 for v in vals if v >= 1.0)
        name = CATEGORIES.get(cat_key, cat_key)
        print(
            f"    {name:25} "
            f"R={sum(vals) / len(vals):.3f}  "
            f"perfect={perfect}/{len(vals)}"
        )
    perfect_total = sum(1 for r in all_recall if r >= 1.0)
    zero_total = sum(1 for r in all_recall if r == 0)
    print()
    print("  DISTRIBUTION:")
    denom = max(len(all_recall), 1)
    print(f"    Perfect (1.0):  {perfect_total:4}  ({100 * perfect_total / denom:.1f}%)")
    print(f"    Zero (0.0):     {zero_total:4}  ({100 * zero_total / denom:.1f}%)")
    print(f"{'=' * 60}\n")

    summary = {
        "kappa": kappa,
        "top_k": top_k,
        "total_items": len(items),
        "avg_recall": avg_recall,
        "per_category": {
            CATEGORIES.get(k, k): sum(v) / len(v) for k, v in per_category.items()
        },
        "elapsed_sec": elapsed,
    }
    if out_file:
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print(f"  Summary written to {out_file}")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="johnny-five x ConvoMem")
    parser.add_argument(
        "--category",
        action="append",
        default=None,
        help="Category (repeatable). Default: all 6 categories.",
    )
    parser.add_argument("--limit", type=int, default=50, help="Items per category (default 50)")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--kappa", type=float, default=0.30)
    parser.add_argument("--cache-dir", default="/tmp/convomem-cache")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    categories = args.category if args.category else list(CATEGORIES.keys())
    run_benchmark(
        categories=categories,
        limit_per_cat=args.limit,
        top_k=args.top_k,
        kappa=args.kappa,
        cache_dir=args.cache_dir,
        out_file=args.out,
    )
