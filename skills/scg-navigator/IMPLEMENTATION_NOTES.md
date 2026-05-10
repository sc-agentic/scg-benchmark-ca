# Implementation notes — scg-navigator

Brief record of design decisions and dead ends, so the next person doesn't re-derive them.

## Skill mode is intentionally graph-only

In `agent.py`, Skill mode wires `tools=[]` and `allowed_tools = list(mcp_tool_names)` — the agent only sees the SCG MCP tools (no `Read`/`Grep`/`Glob`).

**This is intentional, not a bug.** The skill is the *lean graph navigator* variant; its value comes from disciplined graph traversal. If you give it baseline tools too, it becomes a heavier hybrid that loses its cost-efficiency identity.

The skill body's "Fall back to Read/Grep/Glob" section is mostly aspirational guidance about when source matters — but the *implementation* of falling back is what `Baseline` mode is for. The benchmark already measures that as a separate mode for clean comparison.

## What we tried (and why it didn't work)

**Iteration v2** (May 2026): added explicit `Read` guidance to W3 for class-purpose questions (after observing Q8 hierarchy answers consistently lost 0.20–0.30 score vs Baseline because purpose descriptions like "ThumbnailImageViewTarget avoids extra requestLayout calls" require Javadoc, not graph edges).

The change was made in two parts:
1. `SKILL.md` — added W3 step 3 telling the agent to Read source for the 1–3 central classes when describing class purpose.
2. `agent.py` — exposed `Read`/`Grep`/`Glob` to Skill mode so the new instruction would be executable.

**Result on Q8 × Sonnet 4.6 × n=5 (vs. v1 baseline n=5):**

| Config | Score μ | Read calls μ | Tools μ | Weighted μ |
|---|---|---|---|---|
| v1 (graph-only) | 0.60 | 0 | 9.2 | 56,753 |
| v2 (new prose, no Read tool) | 0.60 | 0 | 12.0 | 83,073 |
| v2b (new prose + Read tool) | 0.60 | 13.6 | 21.6 | 104,651 |

Score didn't move. v2b's cost rose to ~Baseline's 101k while keeping Skill's 0.60 — strictly worse than v1. The agent over-applied the Read guidance (13.6 calls vs the skill's "1–3 classes" hint). Adding Read gave the model more text to potentially mis-summarize, not less room to err.

**Conclusion**: Q8's hierarchy weakness isn't an "agent doesn't have Read" problem — it's a "synthesis from rich detail" problem that more tools don't solve. v1 Skill at 0.60 / 56k is already the right cost-quality point for the lean graph variant.

Both changes were reverted. The 10 experimental rows (ids 156–165) were deleted from `results.db` to keep the canonical n=5 cell intact.

## What this implies for future iterations

- **Skill mode keeps its "graph-only" character.** Don't expose baseline tools.
- **For real correctness improvements on Q8-style questions**, the right lever is new MCP tools that do the synthesis server-side, not skill prose. See v3 below for what worked.
- **If a future iteration tries skill prose changes again**, validate at n≥5 on Q8 specifically before drawing conclusions, and watch the Read-call count to verify the agent actually executes the guidance vs just acknowledges it.

## v3 (May 2026) — pre-rendered ASCII tree in `get_class_hierarchy`

After v2 confirmed the bottleneck was *synthesis*, not *insufficient context*, the next iteration moved synthesis out of the model entirely:

- `scg-mcp/src/mcp_server.py` `get_class_hierarchy` now captures `pnodes[-2].id` (the parent in the path) from each variable-length match and renders a proper ASCII tree (`├──`, `└──`, `│   `) before returning.
- Two new helpers — `_render_descendant_tree` and `_render_ancestor_tree` — build a parent→children map and walk it. The agent receives a tree it can quote verbatim instead of an edge list it has to group.

**Result on Q8 × Sonnet 4.6 × Skill × n=5:**

| Config | Score μ | Tools μ | Weighted μ | Cost / 1.0 score |
|---|---|---|---|---|
| v1 (flat edge list) | 0.60 | 9.2 | 56,752 | 94,587 |
| **v3 (pre-rendered tree)** | **0.70** | 11.0 | **51,554** | **73,648 ⭐** |

Score +0.10 (+17%), cost −9%, cost-per-correctness −22%. Aggregate Sonnet 4.6 Skill (n=15 across Q8/Q10/Q15) moves from 0.70 / 83,772 / 119,675 to **0.733 / 82,039 / 111,921**.

**Why this worked when v2 didn't**: v2 added more *input* for the model to synthesize from (Read access). v3 removed the synthesis step entirely — the tool returns the answer-shape, and the model only has to quote it. This validates the v2 conclusion ("synthesis is the problem") and identifies the actual fix ("pre-render in the tool"). Generalizes: any rigid output structure the model has to derive from graph edges is a candidate for server-side rendering.

**Rows in `results.db`**: v3 occupies ids 166–170. v1 rows (≤155) are kept as historical baseline. Plot regeneration would need to filter v1 Q8 out or re-run the full sweep.
