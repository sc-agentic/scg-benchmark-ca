import argparse
import json
import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import seaborn as sns

# ── palette ──────────────────────────────────────────────────────────────────
PALETTE = {"Baseline": "#4C9BE8", "MCP": "#E8834C", "Skill": "#6BBF59", "Skill+Tools": "#9D6BBF"}
sns.set_theme(style="whitegrid", font_scale=1.05)


# ── helpers ──────────────────────────────────────────────────────────────────


def _save(fig: plt.Figure, path: Path, name: str) -> None:
    fig.tight_layout()
    fig.savefig(path / name, dpi=140)
    plt.close(fig)
    print(f"  saved: {name}")


def _fmt_k(x, _=None):
    """Axis formatter: 12 000 → '12 k'."""
    return f"{x / 1000:.0f}k" if x >= 1000 else str(int(x))


def _explode_tool_calls(df: pd.DataFrame) -> pd.DataFrame:
    """Return a long-form DataFrame with one row per individual tool call."""
    rows = []
    for _, run in df.iterrows():
        if not run["tool_calls_log"]:
            continue
        try:
            calls = json.loads(run["tool_calls_log"])
        except (json.JSONDecodeError, TypeError):
            continue
        for call in calls:
            rows.append(
                {
                    "run_id": run["id"],
                    "Agent Mode": run["Agent Mode"],
                    "project_name": run["project_name"],
                    "query_id": run["query_id"],
                    "model_name": run["model_name"],
                    "tool_name": call.get("tool_name", "unknown"),
                    "result_length": call.get("result_length", 0),
                    "result_tokens": call.get("result_length", 0)
                    // 4,  # rough estimate
                    "truncated": bool(call.get("truncated", False)),
                    "error": call.get("error") is not None,
                }
            )
    return pd.DataFrame(rows)


# ── plot functions ────────────────────────────────────────────────────────────

# 1 ─ Correctness ─────────────────────────────────────────────────────────────


def plot_correctness(df: pd.DataFrame, out: Path) -> None:
    col = "correctness_score"
    if col not in df.columns or df[col].isnull().all():
        return

    # -1.0 is the sentinel for "not yet evaluated"; drop those rows
    df = df[df[col] >= 0].copy()
    if df.empty:
        print("  [skip] no evaluated correctness scores found (all -1.0).")
        return

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    # By project
    sns.barplot(
        data=df,
        x="project_name",
        y=col,
        hue="Agent Mode",
        palette=PALETTE,
        errorbar="sd",
        ax=axes[0],
    )
    axes[0].set_title("Correctness Score by Project")
    axes[0].set_xlabel("Project")
    axes[0].set_ylabel("Score (avg ± sd)")
    axes[0].set_ylim(0, 1.1)

    # Overall
    sns.barplot(
        data=df,
        x="Agent Mode",
        y=col,
        hue="Agent Mode",
        palette=PALETTE,
        errorbar="sd",
        legend=False,
        ax=axes[1],
    )
    axes[1].set_title("Overall Correctness Score")
    axes[1].set_xlabel("")
    axes[1].set_ylabel("Score (avg ± sd)")
    axes[1].set_ylim(0, 1.1)

    _save(fig, out, "01_correctness.png")

    # 1b ─ Per-query × per-mode breakdown ─ shows which queries each config
    # struggles on, with all 4 modes side-by-side per query.
    fig, ax = plt.subplots(figsize=(13, 5))
    query_order = sorted(df["query_id"].unique(), key=lambda q: int(q.lstrip("DQT")))
    mode_order = [m for m in ("Baseline", "MCP", "Skill", "Skill+Tools")
                  if m in df["Agent Mode"].unique()]
    sns.barplot(
        data=df,
        x="query_id",
        y=col,
        hue="Agent Mode",
        order=query_order,
        hue_order=mode_order,
        palette=PALETTE,
        errorbar="sd",
        ax=ax,
    )
    ax.set_title("Correctness Score by Query × Agent Mode")
    ax.set_xlabel("Query ID")
    ax.set_ylabel("Score (avg ± sd)")
    ax.set_ylim(0, 1.1)
    ax.legend(title="Agent Mode", loc="lower right")
    _save(fig, out, "01b_correctness_by_query.png")


# 2 ─ Token Overview ──────────────────────────────────────────────────────────


def plot_token_overview(df_c: pd.DataFrame, out: Path) -> None:
    """Prompt vs completion total, boxplot per project, tokens/iteration."""
    if df_c.empty:
        return

    # 2a  weighted tokens box by project — input-equivalent cost
    fig, ax = plt.subplots(figsize=(12, 5))
    sns.boxplot(
        data=df_c,
        x="project_name",
        y="weighted_tokens",
        hue="Agent Mode",
        palette=PALETTE,
        showmeans=True,
        meanprops={
            "marker": "X",
            "markeredgecolor": "black",
            "markerfacecolor": "white",
        },
        ax=ax,
    )
    ax.set_title("Weighted Cost per Run — by Project (X = Average)\ninput + output×5 + cache_cr×1.25 + cache_rd×0.10")
    ax.set_ylabel("Weighted tokens (input-equivalent)")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(_fmt_k))
    _save(fig, out, "02a_total_tokens_by_project.png")

    # 2b  weighted tokens per query_id
    fig, ax = plt.subplots(figsize=(14, 5))
    sns.barplot(
        data=df_c,
        x="query_id",
        y="weighted_tokens",
        hue="Agent Mode",
        palette=PALETTE,
        errorbar=None,
        ax=ax,
    )
    ax.set_title("Average Weighted Cost — by Query")
    ax.set_xlabel("Query ID")
    ax.set_ylabel("Avg weighted tokens")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(_fmt_k))
    plt.xticks(rotation=45, ha="right")
    _save(fig, out, "02b_total_tokens_by_query.png")

    # 2c  cost composition: how each billable component contributes to the
    # weighted total per mode. Bars stack the four weighted contributions
    # (input, output×5, cache_cr×1.25, cache_rd×0.10) so the reader sees what
    # the user actually pays for, not just input + output.
    cols = [
        "total_prompt_tokens",
        "total_completion_tokens",
        "total_cache_creation_tokens",
        "total_cache_read_tokens",
    ]
    available = [c for c in cols if c in df_c.columns]
    grp = df_c.groupby("Agent Mode")[available].mean().reset_index()

    weights = {
        "total_prompt_tokens": 1.0,
        "total_completion_tokens": 5.0,
        "total_cache_creation_tokens": 1.25,
        "total_cache_read_tokens": 0.10,
    }
    nice_labels = {
        "total_prompt_tokens": "Input (×1)",
        "total_completion_tokens": "Output (×5)",
        "total_cache_creation_tokens": "Cache create (×1.25)",
        "total_cache_read_tokens": "Cache read (×0.10)",
    }
    component_colors = {
        "total_prompt_tokens": "#5B8DB8",
        "total_completion_tokens": "#E87B4C",
        "total_cache_creation_tokens": "#A38FCB",
        "total_cache_read_tokens": "#9DBF7E",
    }

    fig, ax = plt.subplots(figsize=(8, 5))
    x = np.arange(len(grp))
    w = 0.55
    bottom = np.zeros(len(grp))
    for c in available:
        weighted_vals = grp[c].to_numpy() * weights[c]
        ax.bar(x, weighted_vals, w, bottom=bottom,
               label=nice_labels[c], color=component_colors[c])
        bottom += weighted_vals

    ax.set_xticks(x)
    ax.set_xticklabels(grp["Agent Mode"])
    ax.set_title("Weighted Cost Composition (avg per run)\nbars sum to weighted_tokens")
    ax.set_ylabel("Weighted tokens (input-equivalent)")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(_fmt_k))
    ax.legend(loc="upper right", fontsize=9)
    # annotate totals on top of each bar
    for xi, total in enumerate(bottom):
        ax.text(xi, total + total * 0.01, f"{total / 1000:.1f}k",
                ha="center", fontsize=9, weight="bold")
    _save(fig, out, "02c_prompt_vs_completion.png")

    # 2d  weighted tokens per iteration — context-growth × cost
    df_c = df_c.copy()
    df_c["tokens_per_iter"] = df_c["weighted_tokens"] / df_c["iterations"].clip(lower=1)
    fig, ax = plt.subplots(figsize=(10, 5))
    sns.boxplot(
        data=df_c,
        x="Agent Mode",
        y="tokens_per_iter",
        hue="Agent Mode",
        palette=PALETTE,
        legend=False,
        ax=ax,
    )
    ax.set_title("Weighted Tokens per Iteration (context-growth × cost proxy)")
    ax.set_ylabel("Weighted tokens / iteration")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(_fmt_k))
    _save(fig, out, "02d_tokens_per_iteration.png")

    # 2e Token breakdown by query (stacked bar)
    tool_tokens_per_run = {}
    for _, run in df_c.iterrows():
        t_cum_cost = 0
        if run.get("tool_calls_log"):
            try:
                calls = json.loads(run["tool_calls_log"])
                iters = max(1, run.get("iterations", 1))
                for idx, call in enumerate(calls):
                    # Because LLM is stateless, earlier tool results are re-sent on every subsequent iteration.
                    # We estimate how many iterations this tool result lived in the context window.
                    occurrences = max(1, iters - min(idx, iters - 1))
                    size = call.get("result_length", 0) // 4
                    t_cum_cost += size * occurrences
            except (json.JSONDecodeError, TypeError):
                pass
        tool_tokens_per_run[run["id"]] = t_cum_cost

    df_stack = df_c.copy()
    df_stack["tool_tokens"] = df_stack["id"].map(tool_tokens_per_run)
    df_stack["base_prompt_tokens"] = (
        df_stack["total_prompt_tokens"] - df_stack["tool_tokens"]
    )
    # If the crude mapping overestimates, cap base_prompt at 0
    df_stack["base_prompt_tokens"] = df_stack["base_prompt_tokens"].clip(lower=0)

    grp_stack = (
        df_stack.groupby(["query_id", "Agent Mode"])[
            ["base_prompt_tokens", "tool_tokens", "total_completion_tokens"]
        ]
        .mean()
        .reset_index()
    )

    queries = sorted(df_stack["query_id"].unique())
    x = np.arange(len(queries))

    # Three shades per mode (light/medium/dark) so each stack reads as one mode.
    SHADES = {
        "Baseline":    ("#8FBCE6", "#4C9BE8", "#2D5A88"),
        "MCP":         ("#F1AC88", "#E8834C", "#A8411D"),
        "Skill":       ("#A8DDA0", "#6BBF59", "#3F8230"),
        "Skill+Tools": ("#C4A5DD", "#9D6BBF", "#5E3F88"),
    }
    modes_present = [m for m in ("Baseline", "MCP", "Skill", "Skill+Tools")
                     if m in grp_stack["Agent Mode"].unique()]
    n_modes = len(modes_present)
    width = 0.85 / max(n_modes, 1)

    fig, ax = plt.subplots(figsize=(14, 6))

    for i, mode in enumerate(modes_present):
        mode_data = grp_stack[grp_stack["Agent Mode"] == mode].set_index("query_id")

        base = mode_data.reindex(queries)["base_prompt_tokens"].fillna(0)
        tools = mode_data.reindex(queries)["tool_tokens"].fillna(0)
        comp = mode_data.reindex(queries)["total_completion_tokens"].fillna(0)

        # Center the n_modes group of bars on each query tick.
        offset = (i - (n_modes - 1) / 2) * width
        light, mid, dark = SHADES.get(mode, ("#cccccc", "#888888", "#444444"))

        ax.bar(x + offset, base, width, label=f"Base Prompt ({mode})", color=light)
        ax.bar(x + offset, tools, width, bottom=base, label=f"Tool Results ({mode})", color=mid)
        ax.bar(x + offset, comp, width, bottom=base + tools, label=f"Completion ({mode})", color=dark)

    ax.set_title("Average Token Breakdown by Query")
    ax.set_xticks(x)
    ax.set_xticklabels(queries, rotation=45, ha="right")
    ax.set_ylabel("Avg Tokens")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(_fmt_k))
    ax.legend(title="Component (Mode)", bbox_to_anchor=(1.01, 1), loc="upper left")

    _save(fig, out, "02e_token_breakdown_by_query.png")

    # 2f Tabular visualization of Average Weighted Tokens per Query (Image Output)
    # Tri-mode: Baseline / MCP / Skill, with diffs vs. Baseline (the cheapest-
    # baseline-relative comparison the reader usually wants).
    query_stats = (
        df_c.groupby(["query_id", "Agent Mode"])["weighted_tokens"]
        .mean()
        .unstack("Agent Mode")
    )

    if "Baseline" in query_stats:
        modes_present = [m for m in ("Baseline", "MCP", "Skill", "Skill+Tools") if m in query_stats.columns]
        table_data = []
        for q, row in query_stats.iterrows():
            cells = [q]
            base = row.get("Baseline", float("nan"))
            for m in modes_present:
                v = row.get(m, float("nan"))
                cells.append(f"{v:,.0f}" if not pd.isna(v) else "N/A")
            # Diff columns: each non-baseline mode minus Baseline
            for m in modes_present:
                if m == "Baseline":
                    continue
                v = row.get(m, float("nan"))
                d = v - base if not pd.isna(base) and not pd.isna(v) else float("nan")
                cells.append(f"{d:+,.0f}" if not pd.isna(d) else "N/A")
            table_data.append(cells)

        col_labels = ["Query ID"] + modes_present + [
            f"Diff ({m} - Base)" for m in modes_present if m != "Baseline"
        ]

        fig, ax = plt.subplots(figsize=(2 + 1.5 * len(col_labels), 0.8 + 0.35 * len(table_data)))
        ax.axis("tight")
        ax.axis("off")
        ax.set_title(
            "Average Weighted Cost (input-equivalent tokens) by Query",
            weight="bold", size=14, pad=15,
        )

        table = ax.table(
            cellText=table_data,
            colLabels=col_labels,
            loc="center",
            cellLoc="center",
        )
        table.auto_set_font_size(False)
        table.set_fontsize(11)
        table.scale(1.2, 1.8)

        # Style headers
        for (row, col), cell in table.get_celld().items():
            if row == 0:
                cell.set_text_props(weight="bold")
                cell.set_facecolor("#e0e0e0")

        _save(fig, out, "02f_query_token_table.png")


# 3 ─ Tool-Level Analysis ─────────────────────────────────────────────────────


def plot_tool_analysis(df_c: pd.DataFrame, out: Path) -> None:
    tc = _explode_tool_calls(df_c)
    if tc.empty:
        print("  [skip] no tool_calls_log data found.")
        return

    # 3a  call frequency per tool, split by mode
    fig, ax = plt.subplots(figsize=(13, 5))
    freq = tc.groupby(["tool_name", "Agent Mode"]).size().reset_index(name="calls")
    # sort tools by total call count descending
    order = (
        freq.groupby("tool_name")["calls"]
        .sum()
        .sort_values(ascending=False)
        .index.tolist()
    )
    sns.barplot(
        data=freq,
        x="tool_name",
        y="calls",
        hue="Agent Mode",
        palette=PALETTE,
        order=order,
        ax=ax,
    )
    ax.set_title("Total Tool Call Frequency")
    ax.set_xlabel("Tool")
    ax.set_ylabel("# Calls")
    plt.xticks(rotation=40, ha="right")
    _save(fig, out, "03a_tool_call_frequency.png")

    # 3b  median result size (tokens) per tool per mode
    fig, ax = plt.subplots(figsize=(13, 5))
    sns.barplot(
        data=tc,
        x="tool_name",
        y="result_tokens",
        hue="Agent Mode",
        palette=PALETTE,
        order=order,
        estimator=np.median,
        errorbar=("pi", 50),
        ax=ax,
    )
    ax.set_title("Median Result Size per Tool (estimated tokens, IQR error bars)")
    ax.set_xlabel("Tool")
    ax.set_ylabel("Result tokens (median)")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(_fmt_k))
    plt.xticks(rotation=40, ha="right")
    _save(fig, out, "03b_tool_result_size.png")

    # 3c  result size distribution (box) — MCP-only deep-dive
    mcp_tc = tc[tc["Agent Mode"] == "MCP"]
    if not mcp_tc.empty:
        fig, ax = plt.subplots(figsize=(13, 5))
        mcp_order = (
            mcp_tc.groupby("tool_name")["result_tokens"]
            .median()
            .sort_values(ascending=False)
            .index.tolist()
        )
        sns.boxplot(
            data=mcp_tc,
            x="tool_name",
            y="result_tokens",
            order=mcp_order,
            color=PALETTE["MCP"],
            ax=ax,
        )
        ax.set_title("MCP: Result-Size Distribution per Tool")
        ax.set_xlabel("Tool")
        ax.set_ylabel("Result tokens (est.)")
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(_fmt_k))
        plt.xticks(rotation=40, ha="right")
        _save(fig, out, "03c_mcp_result_size_distribution.png")

    # 3d  truncation rate per tool
    trunc = (
        tc.groupby(["tool_name", "Agent Mode"])
        .apply(lambda g: g["truncated"].mean() * 100, include_groups=False)
        .reset_index(name="truncation_pct")
    )
    fig, ax = plt.subplots(figsize=(13, 5))
    sns.barplot(
        data=trunc,
        x="tool_name",
        y="truncation_pct",
        hue="Agent Mode",
        palette=PALETTE,
        order=order,
        ax=ax,
    )
    ax.set_title("Truncation Rate per Tool (%)")
    ax.set_xlabel("Tool")
    ax.set_ylabel("% of calls truncated")
    ax.set_ylim(0, 105)
    plt.xticks(rotation=40, ha="right")
    _save(fig, out, "03d_tool_truncation_rate.png")

    # 3e  scatter: weighted cost per run vs tool calls count, coloured by mode
    fig, ax = plt.subplots(figsize=(9, 6))
    for mode, grp in df_c.groupby("Agent Mode"):
        ax.scatter(
            grp["total_tool_calls"],
            grp["weighted_tokens"],
            label=mode,
            color=PALETTE[mode],
            alpha=0.6,
            s=60,
        )
    ax.set_title("Weighted Cost vs # Tool Calls (per run)")
    ax.set_xlabel("# Tool calls")
    ax.set_ylabel("Weighted tokens")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(_fmt_k))
    ax.legend(title="Mode")
    _save(fig, out, "03e_tokens_vs_tool_calls.png")

    # 3f  cumulative result bytes flowing into context per mode (bar)
    ctx = (
        tc.groupby(["run_id", "Agent Mode"])["result_tokens"]
        .sum()
        .reset_index(name="context_from_tools")
    )
    fig, ax = plt.subplots(figsize=(8, 5))
    sns.boxplot(
        data=ctx,
        x="Agent Mode",
        y="context_from_tools",
        hue="Agent Mode",
        palette=PALETTE,
        legend=False,
        ax=ax,
    )
    ax.set_title("Total Tool-Result Tokens Injected into Context (per run)")
    ax.set_ylabel("Tool result tokens (est.)")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(_fmt_k))
    _save(fig, out, "03f_context_from_tools.png")


# 4 ─ Timing & Iterations ─────────────────────────────────────────────────────


def plot_timing(df_c: pd.DataFrame, out: Path) -> None:
    if df_c.empty:
        return

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    sns.boxplot(
        data=df_c,
        x="project_name",
        y="duration_seconds",
        hue="Agent Mode",
        palette=PALETTE,
        ax=axes[0],
    )
    axes[0].set_title("Execution Time by Project")
    axes[0].set_ylabel("Seconds")

    sns.boxplot(
        data=df_c,
        x="project_name",
        y="iterations",
        hue="Agent Mode",
        palette=PALETTE,
        ax=axes[1],
    )
    axes[1].set_title("Iterations by Project")
    axes[1].set_ylabel("Iterations")

    _save(fig, out, "04_timing_and_iterations.png")


# 5 ─ Status Distribution ─────────────────────────────────────────────────────


def plot_status(df: pd.DataFrame, out: Path) -> None:
    status_counts = (
        df.groupby(["Agent Mode", "status"]).size().reset_index(name="count")
    )
    fig, ax = plt.subplots(figsize=(9, 5))
    sns.barplot(
        data=status_counts,
        x="status",
        y="count",
        hue="Agent Mode",
        palette=PALETTE,
        ax=ax,
    )
    ax.set_title("Run Status Distribution")
    ax.set_ylabel("# Runs")
    _save(fig, out, "05_status_distribution.png")


# ── main ──────────────────────────────────────────────────────────────────────


def plot_benchmark_results(db_path: str, output_dir: str, model_filter: str | None = None, project_filter: str | None = None) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query("SELECT * FROM benchmark_runs", conn)
    conn.close()

    if df.empty:
        print("No data found in the database.")
        return

    if model_filter:
        before = len(df)
        df = df[df["model_name"] == model_filter].copy()
        print(f"Filtered to model={model_filter}: {len(df)} of {before} runs.")
        if df.empty:
            print(f"No runs match model_name={model_filter!r}.")
            return

    if project_filter:
        before = len(df)
        df = df[df["project_name"] == project_filter].copy()
        print(f"Filtered to project={project_filter}: {len(df)} of {before} runs.")
        if df.empty:
            print(f"No runs match project_name={project_filter!r}.")
            return

    # Four-state mode label. builtin_tools_enabled splits Skill into Skill (MCP-only
     # tools) and Skill+Tools (MCP + Read/Grep/Glob). Older rows lack the column and
    # default to 0.
    skill_col = df["skill_enabled"] if "skill_enabled" in df.columns else 0
    tools_col = df["builtin_tools_enabled"] if "builtin_tools_enabled" in df.columns else 0

    def _label(is_mcp: int, is_skill: int, has_tools: int) -> str:
        if is_skill and has_tools:
            return "Skill+Tools"
        if is_skill:
            return "Skill"
        if is_mcp:
            return "MCP"
        return "Baseline"

    skill_iter = skill_col if hasattr(skill_col, "__iter__") else [skill_col] * len(df)
    tools_iter = tools_col if hasattr(tools_col, "__iter__") else [tools_col] * len(df)
    df["Agent Mode"] = [
        _label(int(m), int(s), int(t))
        for m, s, t in zip(df["is_mcp_enabled"], skill_iter, tools_iter)
    ]

    # Anthropic billing weights (relative to base input rate):
    #   input        : 1.00x
    #   output       : 5.00x
    #   cache_create : 1.25x
    #   cache_read   : 0.10x
    # `weighted_tokens` collapses these into a single input-equivalent number so
    # comparisons reflect what the user actually pays. The unweighted
    # `total_tokens` (input + output only) is left in place for backward
    # compatibility but is misleading on its own.
    cc = df["total_cache_creation_tokens"] if "total_cache_creation_tokens" in df.columns else 0
    cr = df["total_cache_read_tokens"] if "total_cache_read_tokens" in df.columns else 0
    df["weighted_tokens"] = (
        df["total_prompt_tokens"]
        + df["total_completion_tokens"] * 5
        + cc * 1.25
        + cr * 0.10
    )
    df_completed = df[df["status"] == "completed"].copy()

    print(f"\nLoaded {len(df)} runs ({len(df_completed)} completed).")
    print("Generating plots …\n")

    plot_correctness(df, out)
    plot_token_overview(df_completed, out)
    plot_tool_analysis(df_completed, out)
    plot_timing(df_completed, out)
    plot_status(df, out)

    print(f"\nAll plots saved to: {out.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Visualize benchmark results")
    parser.add_argument("--db", default="results.db", help="Path to SQLite database")
    parser.add_argument("--out", default="plots/sonnet-4-6", help="Directory to save plots")
    parser.add_argument(
        "--model",
        default=None,
        help="Restrict plots to runs with this exact model_name (e.g. claude-haiku-4-5). "
        "Default: include all models.",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Restrict plots to runs with this exact project_name (e.g. DayTrader7 or Glide). "
        "Default: include all projects.",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: Database '{args.db}' does not exist.")
        return

    plot_benchmark_results(args.db, args.out, model_filter=args.model, project_filter=args.project)


if __name__ == "__main__":
    main()
