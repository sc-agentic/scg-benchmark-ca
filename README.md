SCG Benchmark — runs over the Claude Agent SDK.

Auth: the SDK spawns the `claude` CLI binary, so it uses whichever credentials
that binary has. Either set `ANTHROPIC_API_KEY` (pay-per-token) or be logged
into `claude` via `claude /login` (subscription, not officially supported for
automation — expect rate limits at high concurrency).

```bash
# Run benchmark (single Claude model)
uv run python main.py -v --mode both --reruns 1 --concurrency 10

uv run python main.py --mode baseline --query-ids Q15 --reruns 1 -vv
uv run python main.py --mode both --reruns 3 -vv --projects Glide

# Override the model
uv run python main.py --model claude-opus-4-7 --judge-model claude-sonnet-4-5

# Re-run the judge for unscored / failed runs
uv run python reevaluate.py -v

# Export results to CSV
uv run python main.py --export-csv results.csv

# Visualize results
uv run python visualize.py
```
