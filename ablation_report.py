import argparse
import sqlite3
from pathlib import Path


REPORT_SQL = """
WITH cfg AS (
    SELECT
        b.id AS run_id,
        b.query_id,
        CASE
            WHEN b.is_mcp_enabled=1 AND b.skill_enabled=1 AND b.builtin_tools_enabled=1 THEN 'mcp+skill+tools'
            WHEN b.is_mcp_enabled=1 AND b.skill_enabled=1 THEN 'mcp+skill'
            WHEN b.is_mcp_enabled=1 AND b.builtin_tools_enabled=1 THEN 'mcp+tools'
            WHEN b.is_mcp_enabled=1 THEN 'mcp'
            ELSE 'baseline'
        END AS config
    FROM benchmark_runs b
),
joined AS (
    SELECT a.variant_name, c.query_id, c.config, a.score
    FROM ablation_runs a
    JOIN cfg c ON c.run_id = a.source_run_id
    WHERE a.score >= 0
)
SELECT variant_name, config, ROUND(AVG(score), 4) AS avg_score, COUNT(*) AS n
FROM joined
GROUP BY variant_name, config
ORDER BY variant_name, config;
"""

PER_QUERY_SQL = """
WITH cfg AS (
    SELECT
        b.id AS run_id,
        b.query_id,
        CASE
            WHEN b.is_mcp_enabled=1 AND b.skill_enabled=1 AND b.builtin_tools_enabled=1 THEN 'mcp+skill+tools'
            WHEN b.is_mcp_enabled=1 AND b.skill_enabled=1 THEN 'mcp+skill'
            WHEN b.is_mcp_enabled=1 AND b.builtin_tools_enabled=1 THEN 'mcp+tools'
            WHEN b.is_mcp_enabled=1 THEN 'mcp'
            ELSE 'baseline'
        END AS config
    FROM benchmark_runs b
)
SELECT a.variant_name, c.query_id, c.config,
       ROUND(AVG(a.score), 4) AS avg_score, COUNT(*) AS n
FROM ablation_runs a
JOIN cfg c ON c.run_id = a.source_run_id
WHERE a.score >= 0
GROUP BY a.variant_name, c.query_id, c.config
ORDER BY a.variant_name, c.query_id, c.config;
"""

ERRORS_SQL = """
SELECT variant_name, COUNT(*) AS n_errors
FROM ablation_runs
WHERE score < 0
GROUP BY variant_name;
"""


def fmt_table(rows: list[dict], cols: list[str]) -> str:
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    sep = "  "
    head = sep.join(c.ljust(widths[c]) for c in cols)
    line = sep.join("-" * widths[c] for c in cols)
    body = "\n".join(sep.join(str(r[c]).ljust(widths[c]) for c in cols) for r in rows)
    return f"{head}\n{line}\n{body}"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="results.db")
    args = p.parse_args()

    if not Path(args.db).exists():
        raise SystemExit(f"DB not found: {args.db}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    err_rows = [dict(r) for r in conn.execute(ERRORS_SQL).fetchall()]
    if err_rows:
        print("Judge errors per variant:")
        print(fmt_table(err_rows, ["variant_name", "n_errors"]))
        print()

    rows = [dict(r) for r in conn.execute(REPORT_SQL).fetchall()]
    if not rows:
        print("No ablation results yet.")
        return

    print("=== Per-variant aggregate score (avg over all queries) ===\n")
    print(fmt_table(rows, ["variant_name", "config", "avg_score", "n"]))

    by_variant: dict[str, dict[str, float]] = {}
    for r in rows:
        by_variant.setdefault(r["variant_name"], {})[r["config"]] = r["avg_score"]

    print("\n=== Gap analysis (baseline minus mcp+skill) ===\n")
    gap_rows = []
    for v, m in sorted(by_variant.items()):
        b = m.get("baseline")
        ms = m.get("mcp+skill")
        if b is None or ms is None:
            continue
        gap_rows.append(
            {
                "variant_name": v,
                "baseline": f"{b:.4f}",
                "mcp+skill": f"{ms:.4f}",
                "gap": f"{b - ms:+.4f}",
                "gap_%_of_baseline": f"{(b - ms) / b * 100:.1f}%" if b > 0 else "n/a",
            }
        )
    print(
        fmt_table(
            gap_rows,
            ["variant_name", "baseline", "mcp+skill", "gap", "gap_%_of_baseline"],
        )
    )

    per_q = [dict(r) for r in conn.execute(PER_QUERY_SQL).fetchall()]
    if per_q:
        print("\n=== Per-query breakdown ===\n")
        print(fmt_table(per_q, ["variant_name", "query_id", "config", "avg_score", "n"]))

    conn.close()


if __name__ == "__main__":
    main()
