import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

W_PROMPT, W_COMPLETION, W_CACHE_CREATE, W_CACHE_READ = 1.0, 5.0, 1.25, 0.10

PALETTE = {
    "Baseline": "#4C9BE8",
    "MCP": "#E8834C",
    "Skill": "#6BBF59",
    "Skill+Tools": "#9D6BBF",
}
MODE_ORDER = ["Baseline", "MCP", "Skill", "Skill+Tools"]

QUERY_RELABEL = {
    "Q4": "Q1", "Q8": "Q2", "Q10": "Q3", "Q15": "Q4",
    "DT4": "DT1", "DT8": "DT2", "DT10": "DT3", "DT15": "DT4",
}

sns.set_theme(style="whitegrid", font_scale=1.1)


def _mode(row) -> str:
    if not row["is_mcp_enabled"] and not row["skill_enabled"]:
        return "Baseline"
    if row["is_mcp_enabled"] and not row["skill_enabled"]:
        return "MCP"
    if row["is_mcp_enabled"] and row["skill_enabled"] and not row["builtin_tools_enabled"]:
        return "Skill"
    return "Skill+Tools"


def _clean_tool(name: str) -> str:
    return name.replace("mcp__scg__", "")


def _load(db: str, model: str, project: str) -> pd.DataFrame:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM benchmark_runs "
        "WHERE model_name = ? AND project_name LIKE ? "
        "AND correctness_score IS NOT NULL AND correctness_score >= 0",
        (model, f"%{project}%"),
    ).fetchall()
    con.close()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        raise SystemExit(f"no judged runs for model={model} project~={project} in {db}")
    df["Mode"] = df.apply(_mode, axis=1)
    df["weighted_cost"] = (
        df["total_prompt_tokens"] * W_PROMPT
        + df["total_completion_tokens"] * W_COMPLETION
        + df["total_cache_creation_tokens"] * W_CACHE_CREATE
        + df["total_cache_read_tokens"] * W_CACHE_READ
    )
    df["Query"] = df["query_id"].map(lambda q: QUERY_RELABEL.get(q, q))
    df["Mode"] = pd.Categorical(df["Mode"], categories=MODE_ORDER, ordered=True)
    return df.sort_values(["Mode", "Query"])


def _present_modes(df: pd.DataFrame) -> list[str]:
    return [m for m in MODE_ORDER if (df["Mode"] == m).any()]


def _tool_means(df: pd.DataFrame) -> pd.DataFrame:
    totals: dict[str, Counter] = {m: Counter() for m in df["Mode"].cat.categories}
    n_runs: Counter = Counter()
    for _, run in df.iterrows():
        m = run["Mode"]
        n_runs[m] += 1
        log = run["tool_calls_log"]
        if not log:
            continue
        try:
            calls = json.loads(log)
        except (json.JSONDecodeError, TypeError):
            continue
        for call in calls:
            name = call.get("tool_name") or call.get("name") or call.get("tool")
            if name:
                totals[m][_clean_tool(name)] += 1
    recs = []
    for m, counter in totals.items():
        if not n_runs[m]:
            continue
        for tool, total in counter.items():
            recs.append({"Mode": m, "Tool": tool, "calls_per_run": total / n_runs[m]})
    out = pd.DataFrame(recs)
    out["Mode"] = pd.Categorical(out["Mode"], categories=MODE_ORDER, ordered=True)
    return out


def _save(fig, out: Path, name: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    path = out / name
    fig.savefig(path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"  saved: {path}")


def fig_aggregate(df: pd.DataFrame, out: Path, prefix: str) -> None:
    modes = _present_modes(df)
    fig, (axl, axr) = plt.subplots(1, 2, figsize=(9.5, 4.2))
    sns.barplot(
        data=df, x="Mode", y="correctness_score", order=modes, hue="Mode",
        hue_order=modes, palette=PALETTE, errorbar="sd", legend=False, ax=axl,
    )
    axl.set_xlabel("")
    axl.set_ylabel("Correctness score (mean ± sd)")
    axl.set_ylim(0, 1.05)
    axl.tick_params(axis="x", labelrotation=20)

    sns.barplot(
        data=df, x="Mode", y="weighted_cost", order=modes, hue="Mode",
        hue_order=modes, palette=PALETTE, errorbar="sd", legend=False, ax=axr,
    )
    axr.set_xlabel("")
    axr.set_ylabel("Weighted cost per run\n(input-equivalent tokens)")
    axr.yaxis.set_major_formatter(lambda x, _: f"{x / 1000:.0f}k" if x >= 1000 else f"{x:.0f}")
    axr.tick_params(axis="x", labelrotation=20)
    _save(fig, out, f"{prefix}_aggregate.png")


MODEL_PALETTE = {"Sonnet 4.6": "#3C6E9C", "Haiku 4.5": "#E0A458"}


def fig_cross_model(df_sonnet: pd.DataFrame, df_haiku: pd.DataFrame, out: Path, prefix: str) -> None:
    shared = ["Baseline", "MCP", "Skill"]
    a = df_sonnet[df_sonnet["Mode"].isin(shared)].copy()
    a["Model"] = "Sonnet 4.6"
    b = df_haiku[df_haiku["Mode"].isin(shared)].copy()
    b["Model"] = "Haiku 4.5"
    both = pd.concat([a, b], ignore_index=True)
    both["Mode"] = pd.Categorical(both["Mode"], categories=shared, ordered=True)

    fig, ax = plt.subplots(figsize=(7.6, 4.4))
    sns.barplot(
        data=both, x="Mode", y="correctness_score", order=shared, hue="Model",
        hue_order=["Sonnet 4.6", "Haiku 4.5"], palette=MODEL_PALETTE,
        errorbar="sd", ax=ax,
    )
    ax.set_xlabel("")
    ax.set_ylabel("Correctness score (mean ± sd)")
    ax.set_ylim(0, 1.05)
    ax.legend(title="Agent model", frameon=True)
    _save(fig, out, f"{prefix}_cross_model.png")


def fig_per_query(df: pd.DataFrame, out: Path, prefix: str) -> None:
    modes = _present_modes(df)
    queries = sorted(df["Query"].unique(), key=lambda q: int(q.lstrip("DQT")))
    fig, ax = plt.subplots(figsize=(8.2, 4.4))
    sns.barplot(
        data=df, x="Query", y="correctness_score", order=queries, hue="Mode",
        hue_order=modes, palette=PALETTE, errorbar="sd", ax=ax,
    )
    ax.set_xlabel("")
    ax.set_ylabel("Correctness score (mean ± sd)")
    ax.set_ylim(0, 1.05)
    ax.legend(title="Mode", frameon=True, ncol=len(modes), loc="lower center",
              bbox_to_anchor=(0.5, 1.0))
    _save(fig, out, f"{prefix}_per_query.png")


def fig_tool_usage(df: pd.DataFrame, out: Path, prefix: str) -> None:
    modes = _present_modes(df)
    tm = _tool_means(df)
    order = (tm.groupby("Tool")["calls_per_run"].sum()
             .sort_values(ascending=False).index.tolist())
    fig, ax = plt.subplots(figsize=(8.2, 5.2))
    sns.barplot(
        data=tm, y="Tool", x="calls_per_run", order=order, hue="Mode",
        hue_order=modes, palette=PALETTE, orient="y", ax=ax,
    )
    ax.set_ylabel("")
    ax.set_xlabel("Mean tool calls per run")
    ax.legend(title="Mode", frameon=True)
    _save(fig, out, f"{prefix}_tool_usage.png")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--project", default="Glide")
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--out", required=True, help="output directory (thesis img/)")
    ap.add_argument("--cross-haiku-db", default=None,
                    help="if set, also emit the Sonnet-vs-Haiku cross-model figure "
                         "(this --db/--model is the Sonnet side, --cross-haiku-db the Haiku side)")
    args = ap.parse_args()

    df = _load(args.db, args.model, args.project)
    out = Path(args.out)
    print(f"{args.model} / {args.project}: {len(df)} runs, modes={_present_modes(df)}")
    fig_aggregate(df, out, args.prefix)
    fig_per_query(df, out, args.prefix)

    if args.cross_haiku_db:
        df_haiku = _load(args.cross_haiku_db, "claude-haiku-4-5", args.project)
        fig_cross_model(df, df_haiku, out, "glide")
        print(f"  cross-model figure written (Sonnet vs Haiku, 3 shared modes)")


if __name__ == "__main__":
    main()
