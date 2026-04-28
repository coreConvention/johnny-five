# johnny-five — Best Practices

This is the **human-facing** primer. It explains *why* the discipline rules exist, with citations to the academic and engineering literature behind them. If you're integrating, you'll be tempted to skip this doc — don't. The rules look mechanical but the reasoning behind each one is what makes the system actually useful.

Already integrated and just need the rules? Read [`CLAUDE_MD_SNIPPETS.md`](CLAUDE_MD_SNIPPETS.md). Mid-task and need a quick reference? Read [`AGENT_NOTES.md`](AGENT_NOTES.md).

---

## The core insight: read more than you write

A memory system is judged by what it surfaces, not by what it stores. A DB with 10,000 carefully-stored memories that are never searched is exactly as useful as an empty DB. The discipline that separates "memory system" from "write-only journal" is **searching first**.

**The loop is read → learn → store**, in that order:

1. **Read** existing memories (search) before doing the work
2. **Learn** from what you found — incorporate it, or notice that nothing matched
3. **Store** the new lesson, *informed by what was already there* (so it complements rather than duplicates)

This is the single most important rule. Everything else is implementation detail.

### Why search-first beats store-everything

The "Lost in the Middle" paper ([Liu et al., 2023, arXiv:2307.03172](https://arxiv.org/abs/2307.03172)) showed that LLM performance degrades sharply when relevant information is buried in the middle of long context. The implication for memory: **dumping all memories into context is worse than searching for the top 5–10 most relevant.** This is why `memory_recall` and `memory_search` exist; loading the full DB into Claude's context window would actively harm reasoning.

Anthropic's own MCP guidance reinforces this. From the official client best-practices doc ([modelcontextprotocol.io](https://modelcontextprotocol.io/docs/develop/clients/client-best-practices.md)):

> Loading every tool definition into the model's context window upfront wastes tokens, increases latency, and degrades model performance.

The same principle applies to memories: targeted retrieval > full-context loading.

---

## What to store (and what NOT to)

### Store

| Trigger | Type | Why it's high-signal |
|---|---|---|
| User correction | `feedback` | Every correction is one Claude already failed to look up. Storing it (with searchable terms) makes the next session 1 step closer to never repeating that mistake. |
| User preference | `user` | Behavioral guidance you should apply across sessions. Importance 7–8 keeps it in active recall. |
| Architecture decision | `project` | Decisions persist past their original conversation. The "why" is hard to recover from code; store it. |
| Debugging gotcha | `lesson` | The hardest-won knowledge. A 4-hour debugging session compressed into a paragraph is the highest ROI memory in the entire system. |
| External reference | `reference` | URLs, dashboards, ticket IDs that aren't in the codebase. |
| Cross-cutting insight | `lesson` or `project` | Non-obvious connections between systems. The "★ box" pattern from Brandon's CLAUDE.md — these are J5's sweet spot. |

### Don't store

| Anti-pattern | Why not |
|---|---|
| Code patterns from the current repo | Already there. Read the code. |
| Git history / who changed what | `git log`, `git blame` are authoritative. |
| Ephemeral task state | TodoWrite handles in-conversation state better. |
| Anything in CLAUDE.md | Duplication that drifts. |
| Fix recipes already in commits | The commit message is the source of truth. |
| "I just learned the syntax of X" | Re-derivable from docs/practice. |
| Long verbatim transcripts | Anthropic's memory tool guidance is explicit about this — store *distilled* knowledge, not raw chat logs. |

The principle: store things that **can't be re-derived from the codebase, version control, or external docs**. Knowledge that's expensive to rediscover is high-value memory; knowledge that's cheap to look up is noise.

---

## Importance scoring

The 0–10 scale comes directly from Generative Agents (Park et al., Stanford, 2023, [arXiv:2304.03442](https://arxiv.org/abs/2304.03442)), which prompts the LLM to rate each memory on poignancy. Convention:

| Range | Meaning | Examples |
|---|---|---|
| 0–3 | Trivial, OK to age out fast | "User mentioned the weather" |
| 4–6 | Useful but ageable | "Project uses Yarn instead of npm" |
| 7–8 | Important, want at session start | "Database migrations require a maintenance window" |
| 9 | Critical, user explicitly emphasised | "ALWAYS run lint before commit — we got burned by this last quarter" |
| 10 | Reserve for session-state-before-compaction | "Currently mid-implementation of feat X, blocked on Y" |

**Calibration matters.** If you mark everything 9, the importance signal stops differentiating anything. The aging system's whole job is to demote stale low-importance memories so high-importance ones stay surfaced. Inflating defeats this.

---

## Project scoping

Every J5 tool accepts a `project_dir` parameter. Memories scoped to a directory are only returned by searches with the same scope. **This is load-bearing — get it wrong and your context silently pollutes.**

The MCP spec describes this with the `roots` concept ([modelcontextprotocol.io spec](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)):

> Server-initiated inquiries into URI or filesystem boundaries to operate in.

LangGraph's memory docs recommend the same pattern with `(user_id, agent_id, thread_id)` namespacing ([langgraph docs](https://langchain-ai.github.io/langgraph/concepts/memory/)).

### Common scoping mistakes

1. **Hardcoded paths.** `project_dir="/Users/me/work/foo"` breaks when you move the repo, mount it in Docker, or check it out as a worktree. Always use `$CLAUDE_PROJECT_DIR` or current working directory.
2. **No project_dir at all.** Memories without `project_dir` are global — they show up in every project's recall. This is sometimes correct (genuine cross-project user preferences) but usually a bug.
3. **Worktree path mismatch.** A worktree's path is *not* the parent repo's path. If you stored memories with the parent path and now you're in a worktree, scoping won't match. Fix: pick one canonical path (the parent) and always use it.

---

## Tier-based aging

J5 ages memories through four tiers based on access patterns and importance:

```
hot  ──→  warm  ──→  cold  ──→  archived
 ↑         ↑          ↑
 └─────────┴──────────┘  (re-promoted on access or importance increase)
```

This is a granular variant of MemGPT's three-tier model ([Packer et al., 2023, arXiv:2310.08560](https://arxiv.org/abs/2310.08560)). The MemGPT paper described it as "managing context like virtual memory in operating systems" — pages get promoted on access, demoted when cold. J5's extra tier (warm vs cold vs archive) lets the retrieval pipeline apply different similarity thresholds at each level.

| Tier | Filter | When you'd see it |
|---|---|---|
| Hot | No similarity filter | Most recent / most accessed memories. Default for new stores. |
| Warm | >0.75 cosine similarity required | 30+ days without access. Still searchable but only on tight matches. |
| Cold | >0.90 similarity required | 180+ days, low importance. Surfaces only on near-exact queries. |
| Archived | Excluded from search | Soft-deleted. Recoverable via DB but not by `memory_*` tools. |

The math behind decay traces back to **MemoryBank** ([Zhong et al., 2023, arXiv:2305.10250](https://arxiv.org/abs/2305.10250)), which models memory strength on the Ebbinghaus forgetting curve `R = e^(-t/S)` where `S` = importance proxy. J5's `MEMORY_DECAY_RATE` is the corresponding daily multiplier (default 0.995, ≈0.5% decay per day).

### When to call maintenance manually

- `memory_aging`: monthly or after a long session. Runs the decay cycle and tier transitions.
- `memory_consolidate`: when `memory_stats` shows >30% cold-tier. Clusters cold memories and summarises them, archiving the originals. Reduces noise without losing signal.
- Both run automatically if `MEMORY_AUTO_CONSOLIDATE_ENABLED=true`.

---

## The `forever-keep` pin

Some memories must never be forgotten. The aging system is good at forgetting low-value things; it's bad at recognising "this one is structural and load-bearing." Tag with `forever-keep` and:

- Importance never decays
- Tier never demotes
- Consolidation skips it

**Use sparingly.** If everything is forever-keep, nothing is. Reserve for:

- Core user preferences that define how Claude should behave
- Critical workflow invariants (security rules, architectural constraints)
- Expensive-to-rediscover gotchas (postmortems, hard-won lessons)
- Reference pointers the user must never lose (production URLs, on-call rotations)

This pattern doesn't appear in MemGPT or Generative Agents — it's a J5-specific refinement, defensible on the grounds that **a small fraction of memories are genuinely structural** and the cost of including a "break glass" override is low.

---

## Hook integration (the Claude Code-specific bit)

J5's biggest divergence from the literature: it integrates with Claude Code's hook system. No academic memory system has this surface; this is unique to J5.

| Hook | Event | Purpose |
|---|---|---|
| `session-start-recall.sh` | `SessionStart` | Auto-injects relevant memories as `# Resume Context` |
| `precompact-enforce.sh` | `PreCompact` (command) | Mechanical floor if Claude didn't store rich state |
| `user-prompt-correction.sh` | `UserPromptSubmit` | Auto-search J5 on correction signals |
| `tool-failure-tracker.js` | `PostToolUse` | Advise `memory_search` after 3 same-signature failures |

The combined effect: **memory becomes invisible to Claude in the best way.** Claude doesn't need to remember to call `memory_recall` at session start — it just shows up in the system prompt. Claude doesn't need to remember to search on corrections — the hook does it. Claude *does* still need to remember to store, but the prompt-type compaction hook nudges that, and the command-type fallback writes a mechanical floor if Claude misses the cue.

This is the integration that makes J5 feel like part of Claude Code rather than a tool Claude has to consciously invoke.

---

## When NOT to use J5

- **One-off scripting.** No durable context to remember.
- **Sensitive PII / secrets.** J5's storage is local and unencrypted. Don't store credentials.
- **Real-time collaboration with humans.** J5 is per-machine. Use a shared doc / wiki for cross-human knowledge.
- **Authoritative source-of-truth.** Memory is *context*, not ground truth. The codebase, git history, and external docs are authoritative.

The most common failure mode for new users is treating J5 like a knowledge graph or a shared wiki. It's neither. It's per-machine cross-session continuity for one Claude instance.

---

## Comparison with Anthropic's official memory tool

Anthropic shipped a "memory tool" feature in 2025 ([context-management announcement](https://www.anthropic.com/news/context-management), [memory tool docs](https://docs.claude.com/en/docs/agents-and-tools/tool-use/memory-tool)). Worth knowing how J5 compares:

| | Anthropic memory tool | johnny-five |
|---|---|---|
| Storage backend | File-based (markdown directory) | SQLite + sqlite-vec + FTS5 |
| Retrieval | Filename + heading navigation | Semantic + lexical hybrid search |
| Scoping | Per-conversation default | Per-project via `project_dir` |
| Deployment | Client-implemented | Docker container, MCP server |
| Best for | Light-touch memory in any client | Heavy memory in Claude Code with hook integration |

Anthropic's deliberate choice not to use embeddings is informative: filename navigation works fine up to ~100 files. J5 uses semantic search because it's optimized for the case where memories number in the hundreds-to-thousands — at that scale, filenames stop being navigable and you need vectors.

If you have <100 memories and only use one client, the Anthropic memory tool is simpler. If you're at 500+ memories, multi-project, and live in Claude Code, J5 is the right fit.

---

## Source notes

The MCP citations above are verified against `modelcontextprotocol.io`. The arXiv and Anthropic citations are canonical URLs but should be re-verified before publication if exact quotes are needed — the research environment had limited external fetch capability. URLs are correct; quotes are paraphrased from training knowledge of the original papers.

If you're updating this doc and want to extend a citation, the original papers worth reading in full:

- **MemGPT** (tier-based memory, OS-paging metaphor): [arXiv:2310.08560](https://arxiv.org/abs/2310.08560)
- **Generative Agents** (importance scoring, reflection): [arXiv:2304.03442](https://arxiv.org/abs/2304.03442)
- **MemoryBank** (Ebbinghaus decay): [arXiv:2305.10250](https://arxiv.org/abs/2305.10250)
- **A-MEM** (memory linking, Zettelkasten-style): [arXiv:2502.12110](https://arxiv.org/abs/2502.12110)
- **Lost in the Middle** (why search-first beats full-context): [arXiv:2307.03172](https://arxiv.org/abs/2307.03172)
- **MCP spec** (tool design, scoping, transport): [modelcontextprotocol.io](https://modelcontextprotocol.io/)
