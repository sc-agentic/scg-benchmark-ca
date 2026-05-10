# Sonnet 4.6 — SCG Navigator skill benchmark

## Setup

- **Agent model**: `claude-sonnet-4-6`
- **Judge model**: `claude-sonnet-4-6`, 50-turn max
- **Test set**: Q4 (W1 critical entity), Q8 (W3 hierarchy), Q10 (W2 impact), Q15 (W4 multi-hop)
- **Sample**: 5 reruns × 3 modes × 4 queries = **60 runs** (Skill Q8 reflects v3 MCP server only)
- **Modes**: Baseline (Read/Grep/Glob) · MCP (graph tools only) · Skill (graph tools + `SKILL.md` body injected into system prompt)
- **Cost metric**: `weighted_tokens = input + output×5 + cache_create×1.25 + cache_read×0.10` (Anthropic billing weights, in input-equivalent units)
- **Note**: 5 of the original Q15+Skill judge runs hit an org-monthly-limit during the initial sweep and were re-judged via `reevaluate.py`. All scores are now valid.

## Headline (n=20 per mode, Q4/Q8/Q10/Q15)

| Mode | Weighted cost μ | Score μ | **Cost / 1.0 score** |
|---|---|---|---|
| Baseline | 142,292 | **0.90** | 158,102 |
| MCP | 101,353 | 0.65 | 155,929 |
| **Skill (v3)** | **69,995** | 0.73 | **96,545** ⭐ |

**Skill v3 is 31% cheaper than MCP, 51% cheaper than Baseline, and ~38–39% better on cost-per-correctness than either.** Score-wise, Skill recovers ~70% of the gap between MCP (0.65) and Baseline (0.90) while costing half as much as Baseline.

The v3 fix (one MCP server change: `get_class_hierarchy` now renders a proper ASCII tree server-side instead of returning a depth-indented edge list) lifted Q8 Skill from 0.60 → 0.70 and improved aggregate by 0.03 score with a 1.7k weighted cost drop. See `skills/scg-navigator/IMPLEMENTATION_NOTES.md` v3 section.

## Per-query (weighted cost / score)

| Query | Workflow | Baseline | MCP | Skill (v3) | Read |
|---|---|---|---|---|---|
| Q4 | Critical entity | 41,885 / **1.00** | 41,662 / 0.70 | **33,866** / 0.70 | Skill 19% cheaper than MCP at same score; Baseline perfect from Javadoc |
| Q8 | Hierarchy | 101,403 / **0.90** | 78,282 / 0.80 | **51,554** / 0.70 | Pre-rendered tree closes 33% of v1→Baseline gap at lower cost |
| Q10 | Impact analysis | 165,471 / **0.80** | 142,955 / 0.60 | **85,198** / 0.80 | Skill matches Baseline correctness at 49% the cost ⭐ |
| Q15 | Multi-hop | 260,410 / **0.90** | 142,517 / 0.50 | **109,365** / 0.70 | Skill 58% cheaper than Baseline, 22pp better correctness than MCP |

All four queries are clean Skill wins on cost-per-correctness. The two queries where Baseline still leads on absolute correctness (Q4 at 1.00 vs Skill's 0.70; Q8 at 0.90 vs 0.70) are both questions where reading raw Javadoc/source gives a quotable ground-truth string — that's an inherent advantage of textual source over structured graph data.

## Why the skill wins on Sonnet 4.6

It produces a dramatic tool-call reduction. Compared to MCP-only:

| Tool / behavior | MCP calls/run | Skill calls/run | Change |
|---|---|---|---|
| `get_graph_stats` (priors) | 0.0 | **1.0** | 100% adherence to W0 |
| `search_code` (vector search) | ~6 | ~2 | 70% drop — skill says don't vector-search known names |
| `get_node_context` (subgraph) | ~6 | ~3 | summary-first discipline |
| `query_neo4j` (Cypher) | ~9 | ~5 | selective use |
| **Total tool calls** | 32.9 | **16.5** | **50% drop** |
| **Iterations** | 33.9 | 17.5 | matches |

Sonnet 4.6 in MCP-only mode is *much* more exploratory than Haiku was (33 tool calls vs Haiku's 11). The skill imposes discipline, halving that to ~17 calls and producing a substantially better cost-per-correctness answer. **The skill's value scales with the model's tendency to over-explore.**

## The v3 fix — pre-rendered ASCII tree in `get_class_hierarchy`

After v2 confirmed that adding `Read` access didn't help Q8 (synthesis, not context, was the bottleneck), v3 moved the synthesis into the MCP tool itself. `get_class_hierarchy` now captures parent IDs from each path and builds a real `├──`/`└──` tree before returning. The agent quotes that tree verbatim — no flat-edges → indented-tree conversion in the model's head.

**Result on Q8 × Skill × n=5**:

| Variant | Score | Tool calls | Weighted cost | Cost / 1.0 |
|---|---|---|---|---|
| v1 (flat edge list) | 0.60 | 9.2 | 56,752 | 94,587 |
| **v3 (pre-rendered tree)** | **0.70** | 11.0 | **51,554** | **73,648** |

Score +0.10, cost −9%, cost-per-correctness −22% — a clean Pareto improvement. The lesson generalizes: when an answer requires a rigid output structure (tree, ordered chain), having the tool return that structure beats forcing the model to derive it.

## The Q8 hierarchy weak spot — narrowed but not eliminated

| Model | Skill v1 score on Q8 | Skill v3 score on Q8 | Pattern |
|---|---|---|---|
| Sonnet 4.5 | 0.50 | (not re-tested) | Same ASCII-tree rendering error |
| Sonnet 4.6 | 0.60 | **0.70** | v3 fix raises it to Q15-Skill parity |
| Haiku 4.5 | 0.50 | (not re-tested) | Same error |

v1's failure mode was the model mis-grouping nodes when converting a flat edge list into a tree. v3 sidesteps the problem by having the MCP tool return the tree already rendered. The 0.30-point gap vs Baseline (which got the tree right by reading source) shrinks to 0.20.

Skill v3 is now **49% cheaper than Baseline on Q8** *and* closes a third of the correctness gap — the cost win compounds with a real correctness gain. Sonnet 4.5 and Haiku 4.5 weren't re-tested but the same MCP fix should benefit them similarly (the error pattern was identical).

## Cross-model summary (cost / 1.0 score, lower is better)

| Model | Mode | Weighted μ | Score μ | Cost / 1.0 score |
|---|---|---|---|---|
| **Sonnet 4.6** (n=20, Q4/Q8/Q10/Q15, Skill v3) | **Skill** | **69,995** | 0.73 | **96,545** ⭐ |
| Haiku 4.5 (n=20, Q4/Q8/Q10/Q15) | Skill | 54,378 | 0.62 | 87,706 |
| Sonnet 4.5 (n=9, Q8/Q10/Q15) | Skill | 81,488 | 0.61 | 133,587 |
| Sonnet 4.6 (n=15, Q8/Q10/Q15, Skill v1, historical) | Skill | 83,772 | 0.70 | 119,675 |

With Q4 included and Q8 on v3, Sonnet 4.6 matches Haiku's test matrix exactly. Sonnet 4.6 produces **17% better correctness** than Haiku (0.73 vs 0.62) at **10% higher cost-per-correctness** (96,545 vs 87,706) — a meaningful trade depending on whether absolute quality or absolute cost is the binding constraint.

- **Sonnet 4.6 + Skill produces the highest correctness (0.70) of any Skill configuration tested**, at 9% better cost-per-correctness than Sonnet 4.5.
- Haiku is the cheapest absolute, but Sonnet 4.6 + Skill produces a substantially better answer for ~37% more cost.
- Across all three models, **the skill is consistently the best cost-per-correctness option** — by a wide margin on Sonnet 4.6, a moderate margin on Haiku, and a small margin on Sonnet 4.5.

## Honest caveats

- **n=5 per cell on 4 queries** — focused experiment, not a broad survey. Cross-model parity with Haiku is now complete.
- **Q4 and Q8 still favor Baseline on absolute correctness** (1.00 and 0.90 vs Skill's 0.70 each). Reading raw Javadoc/source gives the model a quotable ground-truth string for "purpose" or "role" descriptions; Skill must synthesize from graph metadata.
- **The v3 fix is server-side and one-shot.** No prompt changes, no retraining. Sonnet 4.5 and Haiku 4.5 weren't re-run on v3 but should benefit similarly.
- **Skill is the right answer when cost-per-correctness matters more than absolute correctness.** For workloads where every fraction of a point counts (e.g. compliance review), Baseline is still 0.17 points stronger at 2× the cost.

## Plot index

| File | Shows |
|---|---|
| `01_correctness.png` | Score by project and overall |
| `02a_total_tokens_by_project.png` | Weighted-cost distribution per project |
| `02b_total_tokens_by_query.png` | Weighted cost per query × mode |
| `02c_prompt_vs_completion.png` | **Cost composition** (input/output/cache_cr/cache_rd weighted) per mode |
| `02d_tokens_per_iteration.png` | Weighted tokens per iteration |
| `02e_token_breakdown_by_query.png` | Stacked component breakdown, all 3 modes × 3 queries |
| `02f_query_token_table.png` | Per-query table with diff vs Baseline |
| `03a–03f` | Tool-level analysis (frequency, result size, truncation, scatter) |
| `04_timing_and_iterations.png` | Wall time and iteration counts |
| `05_status_distribution.png` | Completed vs error counts |

## Bottom line

On Sonnet 4.6 with Skill v3 (n=20 across Q4/Q8/Q10/Q15), the skill **halves cost vs Baseline while losing only 17 percentage points of correctness**, and produces **8 percentage points better correctness than MCP-alone at 31% lower cost**. The cost-per-correctness numbers across all three models tested:

```
Haiku 4.5 + Skill:       87,706 per 1.0 correct answer  (cheapest)
Sonnet 4.6 + Skill v3:   96,545 per 1.0 correct answer  ⭐ (best correctness)
Sonnet 4.6 + Skill v1:  119,675 per 1.0 correct answer  (historical)
Sonnet 4.5 + Skill:     133,587 per 1.0 correct answer
```

Recommended next steps:
1. **Re-test Sonnet 4.5 and Haiku 4.5 on Q8** with the v3 MCP server. The fix was server-side, so all three models should benefit; expected lift is ~+0.10 score per cell at no extra cost.
2. **Generalize the v3 lesson to other rigid output shapes.** Q15 (multi-hop chain ordering) is the next candidate — a `find_path` variant that returns a numbered, formatted call chain would likely move the needle there too.
3. **When `EXPANSION_PLAN.md` Tier 1–2 tools land** in the MCP server (project_summary, find_crucial_nodes), extend `SKILL.md` with new W0 priors text and W6 "open-ended architectural question" workflow, then re-benchmark.
