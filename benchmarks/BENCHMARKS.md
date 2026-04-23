# johnny-five Benchmarks

Reproducible retrieval-recall benchmarks for johnny-five against published baselines.

## LoCoMo (snap-research, 10 conversations, 1986 QA pairs)

Measures retrieval recall at top-10 on session granularity. Evidence is the ground-truth set of sessions that contain the answer; recall = fraction of evidence session IDs present in top-10 retrieved.

### Results

| Configuration | Avg R@10 | Perfect | Partial | Zero | Notes |
|---|---|---|---|---|---|
| johnny-five, `kappa=0.0` (semantic-only) | **60.29%** | 55.3% | 9.7% | 35.0% | Matches mempalace raw baseline within rounding |
| johnny-five, `kappa=0.30` (hybrid, default prod) | **85.17%** | 80.8% | 8.4% | 10.9% | +24.88pp over our own baseline |
| *mempalace raw* (reference) | *60.3%* | — | — | — | From `results_locomo_raw_session_top10_*.json` |
| *mempalace hybrid v5* (reference) | *88.9%* | — | — | — | 3.73pp better than our κ — accounted for below |

### Per-category recall (johnny-five)

| Category | κ=0.0 | κ=0.30 | Delta | Notes |
|---|---|---|---|---|
| Single-hop | 0.590 | 0.703 | +11.3pp | |
| Temporal | 0.692 | 0.882 | +19.0pp | |
| Temporal-inference | 0.460 | 0.655 | +19.5pp | Largest remaining gap |
| Open-domain | 0.581 | 0.880 | +29.9pp | Largest lift — entity-heavy queries |
| Adversarial | 0.619 | 0.913 | +29.4pp | Largest lift alongside open-domain |

### Why we don't match hybrid v5's 88.9%

Mempalace's hybrid v5 stacks three post-retrieval boosts:

1. **Keyword overlap** (multiplicative: `dist *= 1 - 0.50 * overlap`) — this is what johnny-five's `kappa` ports.
2. **Quoted-phrase boost** (multiplicative: `dist *= 1 - 0.60 * quoted_boost`) — triggered by questions containing `'...'` or `"..."`. Not yet in johnny-five.
3. **Person-name boost** (multiplicative: `dist *= 1 - 0.20 * name_boost`) — triggered by capitalized proper nouns in queries. Not yet in johnny-five.

Our 3.73pp shortfall matches the aggregate contribution of (2) + (3) roughly. If we add those two boosts in a future release we should close most of the remaining gap. The core signal — κ keyword overlap — already does the heavy lifting.

**Also note the mechanical difference**: mempalace scales cosine *distance* down (1 - κ·overlap), while johnny-five adds κ·overlap to the *combined score*. Both preserve rank ordering in the same direction but with different sensitivities.

### Reproducing

```bash
# One-time setup
git clone https://github.com/snap-research/locomo.git /tmp/locomo
pip install -e ".[dev]"

# Hybrid (default kappa=0.30)
python benchmarks/locomo_bench.py /tmp/locomo/data/locomo10.json \
    --out benchmarks/results_j5_locomo_hybrid_top10_$(date +%Y%m%d).json

# Raw baseline (kappa=0)
python benchmarks/locomo_bench.py /tmp/locomo/data/locomo10.json \
    --kappa 0.0 \
    --out benchmarks/results_j5_locomo_raw_top10_$(date +%Y%m%d).json

# Smoke test: one conversation only
python benchmarks/locomo_bench.py /tmp/locomo/data/locomo10.json --limit 1
```

Full run elapsed: ~25s on a 2024 laptop CPU. No GPU required; sentence-transformers `all-MiniLM-L6-v2` CPU inference is the dominant cost and caches across the session.

### Notes on methodology

- **Scoring weights**: the benchmark zeroes out `β` (recency), `γ` (frequency), and `δ` (importance) because every ingested session is brand-new with zero accesses and identical importance — those signals carry no information for this benchmark. Only `α` (semantic) and `κ` (lexical) are informative. Johnny-five's *production* weights of `0.45·sem + 0.20·rec + 0.10·freq + 0.25·imp + 0.30·lex` are the default for real memory-recall use.
- **Per-conversation fresh DB**: each of the 10 conversations gets its own in-memory SQLite database; sessions are ingested per conversation and queried before the DB is destroyed. No cross-conversation contamination.
- **No docker, no MCP**: the benchmark uses `claude_memory.retrieval.search.search_memories` directly in-process. This measures the retrieval pipeline, not the transport layer.

### Result files

- `results_j5_locomo_raw_top10_*.json` — per-run summary for `kappa=0.0`.
- `results_j5_locomo_hybrid_top10_*.json` — per-run summary for `kappa=0.30` (production default).

Each contains `{kappa, top_k, granularity, total_qa, avg_recall, per_category, elapsed_sec}`.

## Future benchmarks (not yet run)

- **ConvoMem** (Salesforce, 250 items across 5 categories) — smaller, per-category breakdown useful for regression testing.
- **MemBench** (ACL 2025, 8500 items) — larger scale, covers noisy-distractor robustness.
- **LongMemEval** (500 questions) — the mempalace primary benchmark with a dev/held-out split available.

Each would follow the same adapter pattern as `locomo_bench.py`.
