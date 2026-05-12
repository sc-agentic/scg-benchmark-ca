import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmark_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    project_name    TEXT    NOT NULL DEFAULT '',
    model_name      TEXT    NOT NULL,
    query_id        TEXT    NOT NULL,
    query_category  TEXT    NOT NULL,
    is_mcp_enabled  INTEGER NOT NULL,  -- 0 or 1
    skill_enabled   INTEGER NOT NULL DEFAULT 0,  -- 0 or 1; True ⇒ MCP + SKILL.md injected
    builtin_tools_enabled INTEGER NOT NULL DEFAULT 0,  -- 0 or 1; True ⇒ MCP agent ALSO gets Read/Grep/Glob
    run_number      INTEGER NOT NULL,
    total_prompt_tokens         INTEGER NOT NULL,
    total_completion_tokens     INTEGER NOT NULL,
    total_cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    total_cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    total_tokens            INTEGER NOT NULL,
    total_tool_calls        INTEGER NOT NULL,
    iterations              INTEGER NOT NULL,
    duration_seconds        REAL    NOT NULL,
    final_answer            TEXT,
    tool_calls_log          TEXT,     -- JSON array
    status                  TEXT    NOT NULL,  -- completed | error | max_iterations
    correctness_score       REAL,     -- 0.0 | 0.5 | 1.0 | NULL (not yet judged) | -1 (judge error)
    judge_reasoning         TEXT,
    judge_model             TEXT
);

CREATE TABLE IF NOT EXISTS ablation_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT    NOT NULL,
    source_run_id       INTEGER NOT NULL,                  -- FK to benchmark_runs.id
    variant_name        TEXT    NOT NULL,                  -- e.g., 'baseline_judge', 'opus_judge', 'mcp_judge', 'rubric_judge'
    judge_model         TEXT    NOT NULL,
    judge_has_mcp       INTEGER NOT NULL DEFAULT 0,        -- 0 or 1
    uses_rubric         INTEGER NOT NULL DEFAULT 0,        -- 0 or 1
    score               REAL,                              -- 0.0..1.0 (5-point if rubric) or -1 on error
    reasoning           TEXT,
    judge_prompt_tokens     INTEGER DEFAULT 0,
    judge_completion_tokens INTEGER DEFAULT 0,
    UNIQUE(source_run_id, variant_name)
);

CREATE INDEX IF NOT EXISTS idx_ablation_variant ON ablation_runs(variant_name);
CREATE INDEX IF NOT EXISTS idx_ablation_source  ON ablation_runs(source_run_id);
"""


class DatabaseManager:
    def __init__(self, db_path: str = "results.db") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        existing = {
            r[1]
            for r in self._conn.execute("PRAGMA table_info(benchmark_runs)").fetchall()
        }
        migrations = [
            ("correctness_score", "REAL"),
            ("judge_reasoning", "TEXT"),
            ("judge_model", "TEXT"),
            ("project_name", "TEXT DEFAULT ''"),
            ("total_cache_creation_tokens", "INTEGER DEFAULT 0"),
            ("total_cache_read_tokens", "INTEGER DEFAULT 0"),
            ("skill_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("builtin_tools_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ]
        for col, typ in migrations:
            if col not in existing:
                self._conn.execute(f"ALTER TABLE benchmark_runs ADD COLUMN {col} {typ}")
        self._conn.commit()

    def save_run(
        self,
        *,
        project_name: str = "",
        model_name: str,
        query_id: str,
        query_category: str,
        is_mcp_enabled: bool,
        skill_enabled: bool = False,
        builtin_tools_enabled: bool = False,
        run_number: int,
        total_prompt_tokens: int,
        total_completion_tokens: int,
        total_cache_creation_tokens: int = 0,
        total_cache_read_tokens: int = 0,
        total_tokens: int,
        total_tool_calls: int,
        iterations: int,
        duration_seconds: float,
        final_answer: str | None,
        tool_calls_log: list[dict[str, Any]] | None = None,
        status: str = "completed",
        correctness_score: float | None = None,
        judge_reasoning: str | None = None,
        judge_model: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO benchmark_runs (
                timestamp, project_name, model_name, query_id, query_category,
                is_mcp_enabled, skill_enabled, builtin_tools_enabled, run_number,
                total_prompt_tokens, total_completion_tokens,
                total_cache_creation_tokens, total_cache_read_tokens,
                total_tokens, total_tool_calls, iterations, duration_seconds,
                final_answer, tool_calls_log, status,
                correctness_score, judge_reasoning, judge_model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                project_name,
                model_name,
                query_id,
                query_category,
                int(is_mcp_enabled),
                int(skill_enabled),
                int(builtin_tools_enabled),
                run_number,
                total_prompt_tokens,
                total_completion_tokens,
                total_cache_creation_tokens,
                total_cache_read_tokens,
                total_tokens,
                total_tool_calls,
                iterations,
                round(duration_seconds, 3),
                final_answer,
                json.dumps(tool_calls_log) if tool_calls_log else None,
                status,
                correctness_score,
                judge_reasoning,
                judge_model,
            ),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def update_score(
        self,
        run_id: int,
        *,
        correctness_score: float,
        judge_reasoning: str,
        judge_model: str,
    ) -> None:
        self._conn.execute(
            "UPDATE benchmark_runs SET correctness_score=?, judge_reasoning=?, judge_model=? WHERE id=?",
            (correctness_score, judge_reasoning, judge_model, run_id),
        )
        self._conn.commit()

    def get_unscored_runs(self, include_failed: bool = False) -> list[dict[str, Any]]:
        """Return completed runs that have not yet been successfully scored.

        Args:
            include_failed: If True, also returns runs with correctness_score = -1.0
                            (judge parse/API errors) so they can be retried.
        """
        if include_failed:
            condition = "(correctness_score IS NULL OR correctness_score = -1.0)"
        else:
            condition = "correctness_score IS NULL"
        rows = self._conn.execute(
            f"SELECT * FROM benchmark_runs WHERE {condition} AND status='completed' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_runs(
        self,
        *,
        query_id: str | None = None,
        model_name: str | None = None,
        is_mcp_enabled: bool | None = None,
        skill_enabled: bool | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []

        if query_id is not None:
            clauses.append("query_id = ?")
            params.append(query_id)
        if model_name is not None:
            clauses.append("model_name = ?")
            params.append(model_name)
        if is_mcp_enabled is not None:
            clauses.append("is_mcp_enabled = ?")
            params.append(int(is_mcp_enabled))
        if skill_enabled is not None:
            clauses.append("skill_enabled = ?")
            params.append(int(skill_enabled))

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM benchmark_runs{where} ORDER BY id", params
        ).fetchall()
        return [dict(r) for r in rows]

    def save_ablation_result(
        self,
        *,
        source_run_id: int,
        variant_name: str,
        judge_model: str,
        judge_has_mcp: bool,
        uses_rubric: bool,
        score: float,
        reasoning: str,
        judge_prompt_tokens: int = 0,
        judge_completion_tokens: int = 0,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO ablation_runs (
                timestamp, source_run_id, variant_name, judge_model,
                judge_has_mcp, uses_rubric, score, reasoning,
                judge_prompt_tokens, judge_completion_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_run_id, variant_name) DO UPDATE SET
                timestamp=excluded.timestamp,
                judge_model=excluded.judge_model,
                judge_has_mcp=excluded.judge_has_mcp,
                uses_rubric=excluded.uses_rubric,
                score=excluded.score,
                reasoning=excluded.reasoning,
                judge_prompt_tokens=excluded.judge_prompt_tokens,
                judge_completion_tokens=excluded.judge_completion_tokens
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                source_run_id,
                variant_name,
                judge_model,
                int(judge_has_mcp),
                int(uses_rubric),
                score,
                reasoning,
                judge_prompt_tokens,
                judge_completion_tokens,
            ),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_ablation_done(self, variant_name: str) -> set[int]:
        rows = self._conn.execute(
            "SELECT source_run_id FROM ablation_runs WHERE variant_name=? AND score IS NOT NULL AND score >= 0",
            (variant_name,),
        ).fetchall()
        return {r["source_run_id"] for r in rows}

    def export_csv(self, path: str | Path) -> int:
        rows = self._conn.execute("SELECT * FROM benchmark_runs ORDER BY id").fetchall()
        if not rows:
            return 0

        path = Path(path)
        columns = rows[0].keys()
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))
        return len(rows)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "DatabaseManager":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
