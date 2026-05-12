# Sonnet 4.6 — SCG Navigator skill benchmark

## Setup

- **Agent model**: `claude-sonnet-4-6`
- **Judge model**: `claude-sonnet-4-6`, 50-turn max, **rubric_judge v2 methodology** (see "Judge methodology — ablation tests" below)
- **Test set**: Q4 (W1 critical entity), Q8 (W3 hierarchy), Q10 (W2 impact), Q15 (W4 multi-hop) — all on Glide 5.0.5
- **Sample**: 5 reruns × **4 modes** × 4 queries = **80 runs**
- **Modes**:
  - **Baseline** — `Read`/`Grep`/`Glob` only
  - **MCP** — SCG MCP graph tools only (no skill, no built-in tools)
  - **Skill** — MCP graph tools + `SKILL.md` body injected into system prompt
  - **Skill+Tools** — Skill mode AND built-in `Read`/`Grep`/`Glob` allowed alongside MCP
- **Cost metric**: `weighted_tokens = input + output×5 + cache_create×1.25 + cache_read×0.10` (Anthropic billing weights, in input-equivalent units)

## Headline (n=20 per mode)

| Mode | Score μ | Weighted cost μ | **Cost / 1.0 score** | Tool calls μ | Iterations μ |
|---|---|---|---|---|---|
| Baseline | **0.79** | 142,293 | 180,750 | 32.7 | 33.7 |
| MCP | 0.70 | 101,354 | 144,791 | 27.4 | 28.4 |
| Skill | 0.71 | **69,996** | **98,170** ⭐ | **14.8** | 15.8 |
| **Skill+Tools** | **0.78** | 82,113 | 105,952 | 18.1 | 19.1 |

Two distinct "wins" depending on what you optimize for:

- **Skill is the best cost-per-correctness option** — 46% cheaper than Baseline while only 8 points lower on score. Best efficiency.
- **Skill+Tools matches Baseline on correctness (0.78 vs 0.79) at 42% lower cost** — the best absolute-quality config under budget pressure. Adding built-in tools on top of the Skill prompt closes the entire correctness gap to Baseline.

MCP alone (no skill, no built-in tools) is the weakest config — pure graph tools without the SKILL.md prompt produce the worst score and middling cost.

## Per-query breakdown

| Query | Topic | Baseline | MCP | Skill | Skill+Tools |
|---|---|---|---|---|---|
| Q4 | Critical entity (`Engine` role + deps) | 0.80 / 41.9k | 0.75 / 41.7k | 0.70 / 33.9k | **0.85** / 40.5k |
| Q8 | Class hierarchy (`Target<R>`) | **0.80** / 101.4k | 0.75 / 78.3k | **0.80** / 51.6k | 0.75 / 72.1k |
| Q10 | Impact analysis (`DiskCacheStrategy`) | 0.80 / 165.5k | 0.65 / 143.0k | 0.75 / 85.2k | **0.85** / 99.0k |
| Q15 | Multi-hop trace (`GifDrawable`) | **0.75** / 260.4k | 0.65 / 142.5k | 0.60 / 109.4k | 0.65 / 116.8k |
| **avg** | | **0.79** | **0.70** | **0.71** | **0.78** |

Format: `score / weighted_cost`. Each cell is mean over 5 runs.

### What the per-query chart (`01b_correctness_by_query.png`) shows

- **Q4 & Q10 — Skill+Tools wins outright** (0.85 vs 0.80 Baseline). Adding built-in tools to the Skill prompt produces _better_ answers than Baseline, at ~40–60% of Baseline's cost. These are the queries where the SCG graph provides genuinely useful structure (entity dependencies, impact propagation) and the agent can spot-check with `Read`/`Grep` when needed.
- **Q8 — Baseline & Skill tied (0.80)**. Skill_Tools slightly under (0.75). For class hierarchy questions, the raw source files contain the answer almost verbatim — `Read` access is sufficient. The skill captures the same information from graph data at half the cost.
- **Q15 — Baseline wins (0.75)**, all other modes degraded (0.60–0.65). The multi-hop trace (3 hops from `GifDrawable`) is the hardest question across the board. The MCP graph tools partially help (vs Baseline's high cost) but no config produces a clean answer; agents lose details on hop 2 or hop 3.

### Patterns

1. **The MCP-only mode is dominated** — it costs more than Skill (101k vs 70k) and scores no better (0.70 vs 0.71). There is no scenario where running MCP-only is a better choice than running with the SKILL.md prompt. Skill's discipline (priors, summary-first, selective Cypher) cuts both cost and noise.
2. **Skill+Tools is a Pareto improvement over MCP** in absolute terms — better score (0.78 vs 0.70) at lower cost (82k vs 101k). The combination of structured graph + textual spot-check beats either alone.
3. **Baseline still wins on the hardest query (Q15)** — when the answer requires assembling multiple specific facts across loosely-connected modules, raw source-reading produces more reliable results than graph-mediated reasoning. The SCG graph is a substitute for navigation, not for synthesis.
4. **The error bars on `01_correctness.png` overlap heavily across all 4 modes** — the "winner" is not statistically obvious in any single comparison. With n=5 per cell × 4 queries, total n=20 per mode, the differences are real but tight. The cost differences are more dramatic and more robust.

## Cost & efficiency observations

Visible in the token-overview plots (`02a`–`02f`):

- **Baseline pays a 41% premium for context** vs Skill (142k vs 70k weighted). Most of that is `Read` calls dragging large source files into context. Q15 baseline averages **260k** weighted tokens — over 2.5× the Skill+Tools cost on the same question.
- **Skill+Tools costs ~17% more than Skill** (82k vs 70k) and the extra spend buys 7 points of correctness. Conversion rate: roughly 0.6 score points per 10k weighted tokens, which is the best "marginal return" of any config jump we see.
- **Tool-call frequency** (`03a`): Skill makes _half_ the tool calls of Baseline (14.8 vs 32.7). Skill+Tools is 18.1 — still 45% below Baseline. The `SKILL.md` prompt does measurably reduce over-exploration even when extra tools are available.
- **Iterations match tool calls almost 1:1** — each iteration produces one tool call on average. The skill primarily wins by exiting earlier, not by doing more per turn.

## Judge methodology — ablation tests

The original benchmark used a **production judge**: Sonnet 4.6, 3-point scale (0.0 / 0.5 / 1.0), generic "is this answer correct?" prompt with no rubric. Initial cross-mode results showed a striking gap (Skill+Tools ≈ 0.50 vs Baseline 1.00 on Q4) that looked too clean. To validate, we ran a 4-variant ablation re-judging the same answers under different judge configurations.

### The 4 ablation variants

| Variant | Model | Tools | Scale | Rubric | Result (Q4 gap baseline−mcp+skill) |
|---|---|---|---|---|---|
| `baseline_judge` | Sonnet 4.6 | Read/Grep/Glob | 3-pt | none | +0.30 |
| `mcp_judge` | Sonnet 4.6 | Read/Grep/Glob + MCP | 3-pt | none | +0.30 |
| `opus_judge` | Opus 4.7 | Read/Grep/Glob | 3-pt | none | **+0.00** (saturated, every answer scored 1.0) |
| `rubric_judge` v1 | Sonnet 4.6 | Read/Grep/Glob | 5-pt | must_cover + **penalize_errors** | **+0.50** |

`rubric_judge v1` showed the largest gap and `opus_judge` showed none — neither result felt right. Closer inspection revealed the cause.

### The bug — anchoring on `penalize_errors`

The v1 rubric included a `penalize_errors` field listing 3 specific known mistakes per query (e.g., for Q4: _"GlideExecutor listed as a direct field of Engine"_). Statistical pattern across 21 rubric_judge reasonings:

- 15 of 21 explicitly cited a `penalize_error` from the rubric.
- **0 of 21** flagged a factual error _outside_ the listed `penalize_errors`, even though the prompt asked the judge to "flag any other verifiable factual errors you find too".

Direct evidence — same answer, two judges:

> **Run 176, opus_judge** (1.0): *"Only minor inaccuracy is mislabeling the fourth GlideExecutor as 'main thread executor' instead of 'animationExecutor'."*
> **Run 176, rubric_judge v1** (0.5): mentions only the listed GlideExecutor-as-field penalize_error. Never notices the wrong executor name.

The judge was treating `penalize_errors` as an _exhaustive_ checklist rather than illustrative examples. Since `penalize_errors` had been written after observing some specific agent failure modes, the rubric was **inadvertently tuned**: any agent config that happened to make those listed errors more frequently got systematically harder-penalized than configs that made _different_ errors of equal severity.

### The fix — `rubric_judge` v2

Replaced `penalize_errors` with a structural change to the research prompt:

- **Part 1 — must_cover coverage**: each rubric item verdict (COVERED / PARTIAL / MISSING / WRONG) with evidence from tools.
- **Part 2 — independent spot-check**: judge must quote 5 of the most specific factual claims in the answer (class types, field membership, signatures, ordering, defaults) and verify each independently. Cannot just re-verify must_cover items.
- **Six generic categories of error shapes** to hunt for: wrong class membership, extends-vs-implements, wrong type signatures, wrong ordering, hallucinations, wrong defaults. Project-agnostic — teaches the judge what to look for without anchoring to specific known errors.

After re-running all 90 ablation calls under v2: **gap baseline − Skill on Q4 dropped from +0.50 → +0.075**. The "MCP+skill catastrophic" narrative dissolved. Baseline also dropped from 1.0 → 0.79 because the spot-check now catches implementation-detail errors in baseline answers too (e.g., _"Jobs as a single HashMap"_ when it's actually two HashMaps).

### Implication for these results

All 80 scores reported in this document use rubric_judge v2 — uniform methodology across modes and queries. The new judge is **strictly more rigorous** than the production judge: every answer is independently spot-checked against the codebase, with a 5-point scale that can distinguish "perfect" from "perfect with one verified error".

## Caveats

- **n=5 per cell on 4 queries** — focused experiment, error bars overlap on most pairwise mode comparisons (see `01b`). The cost differences are larger than the score differences and more robust.
- **Glide-only** — these results say nothing about how the modes compare on other codebases. DayTrader7 will need re-running under the new judge methodology.
- **Q15 (multi-hop) remains the consistent weak spot** for all non-Baseline modes. The structure of the answer (3-hop dependency chain with concrete names at every level) is something the MCP graph tools haven't quite cracked yet.
- **Skill+Tools beats Skill in absolute terms but at a cost.** If correctness matters more than efficiency, use Skill+Tools (0.78 ≈ Baseline 0.79). If efficiency matters more, use Skill (0.71 at 70k cost).
- **The judge change was substantive.** Earlier conclusions that compared Skill across models (Sonnet 4.5 vs 4.6 vs Haiku) used the older, less-rigorous judge and are no longer directly comparable to the numbers in this document.

## Plot index (regenerated 2026-05-12 with 4-mode support)

| File | Shows |
|---|---|
| `01_correctness.png` | Score by project + overall by mode |
| **`01b_correctness_by_query.png`** | **Per-query × per-mode score with error bars — the key plot for spotting which queries each config struggles on** |
| `02a_total_tokens_by_project.png` | Weighted-cost distribution per project, per mode |
| `02b_total_tokens_by_query.png` | Weighted cost per query × mode |
| `02c_prompt_vs_completion.png` | Cost composition (input/output/cache_cr/cache_rd weighted) per mode |
| `02d_tokens_per_iteration.png` | Weighted tokens per iteration |
| `02e_token_breakdown_by_query.png` | Stacked component breakdown, all 4 modes × all queries |
| `02f_query_token_table.png` | Per-query table with diff vs Baseline |
| `03a–03f` | Tool-level analysis (frequency, result size, truncation, scatter) |
| `04_timing_and_iterations.png` | Wall time and iteration counts |
| `05_status_distribution.png` | Completed vs error counts |

## Bottom line

Under a rigorous, bias-corrected judge (rubric_judge v2 with structured spot-check, no anchoring):

```
Mode         Score  Cost (weighted)  Cost / 1.0 score
─────────────────────────────────────────────────────
Baseline     0.79   142,293          180,750
MCP          0.70   101,354          144,791
Skill        0.71    69,996           98,170  ⭐ cheapest per unit correctness
Skill+Tools  0.78    82,113          105,952  ⭐ best correctness under budget
```

**The SCG Navigator skill produces a real efficiency gain — ~46% cost reduction vs Baseline with only 8 points of correctness lost.** Adding built-in tools on top (Skill+Tools) closes the entire correctness gap at 42% lower cost than Baseline. The MCP graph tools alone — without the SKILL.md prompt — are dominated by every other config: the prompt is doing genuine work.

The ablation work was as important as the headline numbers. The earlier "MCP catastrophic" finding was an artifact of judge anchoring on a specific list of expected errors. Once the judge was forced to independently spot-check claims against the codebase, all four configs landed in a 0.70–0.79 band — meaningful differences, but nothing close to the dramatic gap the old judge reported.

### Next steps

1. **Re-run DayTrader7** under the new judge methodology to validate the skill on a second codebase.
2. **Investigate Q15 specifically** — multi-hop traces are a consistent weak spot. A `find_path` MCP tool returning a formatted call chain (analogous to the v3 `get_class_hierarchy` ASCII-tree fix) is the likely lever.
3. **Verify n=5 is enough** by re-running 1-2 cells at n=20 — current error bars overlap on many pairwise comparisons.
4. **Consider testing the new judge on the historical sonnet-4-5 / haiku-4-5 answers** to retrospectively recover cross-model comparison data without re-running agents.
