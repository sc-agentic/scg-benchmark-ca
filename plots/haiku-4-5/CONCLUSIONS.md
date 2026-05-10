# Haiku 4.5 — SCG Navigator skill benchmark

## Setup

- **Agent model**: `claude-haiku-4-5`
- **Judge model**: `claude-sonnet-4-6`, 50-turn max
- **Test set**: Q4 (W1 critical entity), Q8 (W3 hierarchy), Q10 (W2 impact), Q15 (W4 multi-hop)
- **Sample**: 5 reruns × 3 modes × 4 queries = **60 runs** (+ 3 smoke-test runs on Q10 also folded in: n=63)
- **Modes**: Baseline (Read/Grep/Glob) · MCP (graph tools only) · Skill (graph tools + `SKILL.md` body injected into system prompt)
- **Cost metric**: `weighted_tokens = input + output×5 + cache_create×1.25 + cache_read×0.10` (Anthropic's billing weights, in input-equivalent units)

## Headline (n=20 per mode)

| Mode | Weighted cost μ | Score μ | **Cost / 1.0 score** |
|---|---|---|---|
| Baseline | 92,103 | **0.70** | 131,600 |
| MCP | 61,029 | 0.53 | 115,100 |
| **Skill** | **54,378** | 0.62 | **87,700** ⭐ |

**Skill is 11% cheaper per run than MCP, 41% cheaper than Baseline, and 24% better cost-per-correctness than MCP** (33% better than Baseline). Skill recovers ~half the correctness gap MCP creates vs Baseline while being the cheapest mode.

## Why the skill wins on cost

It steers tool selection exactly the way `SKILL.md` teaches. Compared to MCP-only:

| Behavior the skill prescribes | MCP calls/run | Skill calls/run |
|---|---|---|
| Open with `get_graph_stats` (W0 priors) | 0 | **1.0** (100% adherence) |
| Don't vector-search a known name | 3.2 | 1.4 |
| Summary before context | summary 1.9 / context 1.8 | summary 3.0 / context 1.1 |
| Use `query_neo4j` for literal names | 2.3 | 3.0 |

Iteration count drops 11.3 vs 12.2 (MCP) vs 18.4 (Baseline) — fewer iterations cross fewer prompt-cache breakpoints, so cache_create costs drop too. That's where the cost win compounds.

## Per-query (weighted cost / score)

| Query | Workflow | Baseline | MCP | Skill | Read |
|---|---|---|---|---|---|
| Q4 | Critical entity | 28,825 / **0.80** | 28,138 / 0.50 | 28,954 / 0.70 | Skill recovers 0.20 from MCP regression |
| Q8 | Hierarchy | 68,196 / **0.70** | 59,757 / 0.50 | **39,157** / 0.50 | Skill 43% cheaper at MCP-equivalent score |
| Q10 | Impact | 135,384 / **0.70** | 84,675 / 0.60 | **72,857** / 0.60 | Skill cheapest at MCP-equivalent score |
| Q15 | Multi-hop | 136,005 / 0.60 | 71,546 / 0.50 | 76,544 / **0.70** | Skill best correctness *and* 44% cheaper than Baseline |

## Honest caveats

- **Baseline (Read/Grep) still has the highest absolute correctness.** Reading raw Java source gives Haiku ground-truth text it can quote precisely; MCP returns structured graph data Haiku has to synthesize into prose, which introduces rendering errors. The skill mitigates this but doesn't fully close the gap.
- **Q8 errors are mode-independent.** Both MCP and Skill produced the same ASCII-tree mistake (Drawable/Bitmap-ThumbnailImageViewTarget shown as children of DrawableImageViewTarget instead of ThumbnailImageViewTarget). The graph data is correct; the synthesis isn't. Skill iteration #2 should add explicit "verify each parent edge from `get_class_hierarchy` output before rendering the tree" guidance.
- **Judge variance**: judges occasionally still produced empty research findings even with 50-turn cap. Worth tightening the judge prompt before the next sweep.
- **n=5 is small.** Treat per-query deltas of <0.10 as noise. Aggregate-mode numbers are the load-bearing signal.

## Plot index

| File | Shows |
|---|---|
| `01_correctness.png` | Score by project and overall |
| `02a_total_tokens_by_project.png` | Weighted-cost distribution per project |
| `02b_total_tokens_by_query.png` | Weighted cost per query × mode |
| `02c_prompt_vs_completion.png` | **Cost composition** (input/output/cache_cr/cache_rd weighted) per mode — see where the cost lives |
| `02d_tokens_per_iteration.png` | Weighted tokens per iteration |
| `02e_token_breakdown_by_query.png` | Stacked component breakdown, all 3 modes × 4 queries |
| `02f_query_token_table.png` | Per-query table with diff vs Baseline |
| `03a–03f` | Tool-level analysis (frequency, result size, truncation, scatter) |
| `04_timing_and_iterations.png` | Wall time and iteration counts |
| `05_status_distribution.png` | Completed vs error counts |

## Bottom line

Ship the skill on Haiku for any structural-codebase question workload. It pays for its own ~3.3k tokens of system-prompt overhead many times over — every dollar of Anthropic spend yields ~24% more correct answers than MCP-alone, and ~33% more than Baseline. The skill is doing what we designed it to do: trade a small upfront cost for a much larger downstream tool-call efficiency win.
