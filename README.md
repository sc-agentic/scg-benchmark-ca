SCG Benchmark — runs over the Claude Agent SDK.

Auth: the SDK spawns the `claude` CLI binary, so it uses whichever credentials
that binary has. Either set `ANTHROPIC_API_KEY` (pay-per-token) or be logged
into `claude` via `claude /login` (subscription, not officially supported for
automation — expect rate limits at high concurrency).

Three agent modes:
- `baseline` — Read/Grep/Glob only.
- `mcp` — SCG MCP tools only (no skill).
- `skill` — SCG MCP tools **plus** the `scg-navigator` SKILL.md body injected
  into the system prompt at runtime. Tests whether navigation guidance on top
  of the same tools changes how they're used.

```bash
# Full sweep across all three modes (default)
uv run python main.py -v --mode all --reruns 3 --concurrency 10

# Compare just MCP vs Skill on a single query
uv run python main.py --mode mcp --query-ids Q15 --reruns 1 -vv
uv run python main.py --mode skill --query-ids Q15 --reruns 1 -vv

# Legacy two-mode comparison (baseline+mcp, no skill)
uv run python main.py --mode both --reruns 3 -v

# Point at a different SKILL.md
uv run python main.py --mode skill --skill-path skills/scg-navigator/SKILL.md

# Override the model
uv run python main.py --model claude-opus-4-7 --judge-model claude-sonnet-4-5

# Re-run the judge for unscored / failed runs
uv run python reevaluate.py -v

# Export results to CSV
uv run python main.py --export-csv results.csv

# Visualize results
uv run python visualize.py
```

Skill mode requires the SKILL.md file (default `skills/scg-navigator/SKILL.md`)
and a reachable MCP server — `main.py` pre-flight-checks both and refuses to
run rather than silently degrading.
