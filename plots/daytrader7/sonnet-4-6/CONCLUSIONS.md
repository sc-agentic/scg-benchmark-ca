# DayTrader7 + Sonnet 4.6 — SCG Navigator skill benchmark

First cross-project validation of the skill. Tests whether the cost-per-correctness gains seen on Glide generalize to a different Java codebase (an enterprise Java EE app with a layered web → EJB → JPA architecture).

## Setup

- **Project**: DayTrader7 (`codebases/daytrader7`) — IBM's Java EE 7 stock-trading sample app, 108 Java files, 4,292 nodes / 16,172 edges in the SCG graph.
- **Agent model**: `claude-sonnet-4-6`
- **Judge model**: `claude-sonnet-4-6`, 50-turn max
- **Test set**: DT8 (W3 hierarchy of `TradeServices`), DT10 (W2 impact of `OrderDataBean`). DT4 and DT15 not run (token budget).
- **Sample**: 5 reruns × 3 modes × 2 queries = **30 runs**, 0 errors, concurrency=5
- **Modes**: Baseline (Read/Grep/Glob) · MCP (graph tools only) · Skill (graph tools + `SKILL.md`)
- **MCP server**: v3 (with pre-rendered ASCII tree from May 2026 fix)

## Headline (n=10 per mode, DT8+DT10)

| Mode | Weighted cost μ | Score μ | **Cost / 1.0 score** |
|---|---|---|---|
| Baseline | 161,960 | **0.95** | 170,484 |
| MCP | 110,362 | 0.55 | 200,659 |
| **Skill** | **71,758** | 0.65 | **110,396** ⭐ |

**Skill is 35% cheaper than MCP, 56% cheaper than Baseline, and 35–45% better on cost-per-correctness than either.** Tool calls drop from 26.5 (Baseline) / 25.6 (MCP) to 14.1 (Skill) — a 45% reduction.

The skill's value proposition holds on DayTrader: **cheaper than MCP-alone, with better correctness, on a codebase the skill was never tuned for.**

## Per-query

| Query | Workflow | Baseline | MCP | Skill | Read |
|---|---|---|---|---|---|
| DT8 | Hierarchy | 114,647 / **1.00** | 58,971 / 0.60 | **55,956** / 0.80 | Skill 51% cheaper than Baseline, 33% better correctness than MCP ⭐ |
| DT10 | Impact analysis | 209,272 / **0.90** | 161,753 / 0.50 | **87,559** / 0.50 | Skill matches MCP correctness at 46% the cost; Baseline lead is large here |

**DT8 (hierarchy) is a clean Skill win** — the v3 pre-rendered ASCII tree in `get_class_hierarchy` lands the 4-implementation list (`TradeAction`, `TradeDirect`, `TradeSLSBLocal`, `TradeSLSBRemote`) more often than MCP-alone (0.80 vs 0.60).

**DT10 (impact) is honest about Skill's weakness**: when an entity has wide fan-out across many layers, the "summary-first" tool-call discipline saves cost but misses long-tail dependents. `OrderDataBean` is used in web servlets, EJBs, JDBC direct, JPA direct, JSF beans — Baseline's exhaustive grep wins on completeness (0.90), while both graph modes converge at 0.50.

## Cross-project comparison — Skill mode only

| Project | Query | Workflow | Score | Weighted | Cost/correct |
|---|---|---|---|---|---|
| Glide | Q8 (v3) | Hierarchy | 0.70 | 51,554 | 73,648 |
| **DayTrader** | **DT8** | **Hierarchy** | **0.80** | 55,956 | **69,945** ⭐ |
| Glide | Q10 | Impact | 0.80 | 85,198 | 106,498 |
| DayTrader | DT10 | Impact | 0.50 | 87,559 | 175,118 |

- **Hierarchy questions transfer well**, even slightly better on DayTrader (DT8 0.80 vs Glide Q8 v3 0.70). The pre-rendered tree pays off on both codebases.
- **Impact analysis transfers poorly when fan-out is wide.** DayTrader's `OrderDataBean` reaches every layer; Glide's `DiskCacheStrategy` was more contained. Skill's score drops from 0.80 → 0.50.

## Why the skill still wins on aggregate

Even with the DT10 correctness drop, **Skill is the cost-per-correctness leader on DayTrader by a wide margin** (110,396 vs 170,484 for Baseline, 200,659 for MCP). The tool-call reduction (14.1 vs 25.6 MCP vs 26.5 Baseline) is the load-bearing factor — fewer iterations cross fewer prompt-cache breakpoints, so weighted cost drops faster than score.

## Honest caveats

- **n=10 per mode, 2 queries** — directional signal, not a definitive cross-project claim. Adding DT4 (critical entity) and DT15 (multi-hop) would round out the matrix.
- **DT10 is a regression vs Glide Q10.** Skill goes from 0.80 (Glide) to 0.50 (DayTrader) on the same workflow. The hypothesis is wide fan-out, but n=5 makes this tentative.
- **Baseline at 0.95 aggregate** is striking — DayTrader's Java EE code has rich Javadoc that Baseline can quote directly. Glide had 0.87. Source-quoting remains the highest-fidelity path when raw correctness is the only objective.

## Bottom line

The skill **generalizes from Glide to DayTrader without retuning**:
- On hierarchy (DT8): Skill is the best on cost AND best on cost-per-correctness, beating MCP-alone on score by 0.20.
- On impact (DT10): Skill is cheapest by a wide margin but loses 0.40 score vs Baseline due to wide fan-out.
- Aggregate cost-per-correctness: Skill 110,396 vs Baseline 170,484 vs MCP 200,659.

```
Glide      + Sonnet 4.6 + Skill v3:  96,545 per 1.0 correct answer  (n=20, Q4/Q8/Q10/Q15)
DayTrader7 + Sonnet 4.6 + Skill v3: 110,396 per 1.0 correct answer  (n=10, DT8/DT10)
```

The 14% cost-per-correctness premium on DayTrader is entirely driven by DT10's wide-fanout impact analysis. On the hierarchy question DayTrader is actually 5% *better* than Glide.

## Next steps

1. **Fill DT4 and DT15** when tokens permit (~25 min wall time at concurrency=5).
2. **Address the wide-fanout impact problem**: candidate is a new MCP tool `get_dependents(node_id, edge_types=...)` that returns *every* incoming caller/user in one batched response, so the model doesn't have to enumerate via summary→summary chains.
3. **Re-run DT8 with Haiku** to confirm the cross-model finding from Glide (skill discipline scales with model exploratory tendency).
