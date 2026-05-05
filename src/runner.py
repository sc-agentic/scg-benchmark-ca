import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from .agent import run_agent
from .database import DatabaseManager
from .judge import evaluate_answer
from .models import BenchmarkProject, BenchmarkQuery, RunConfig

log = logging.getLogger(__name__)


def load_projects(path: str | Path) -> list[BenchmarkProject]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))

    projects: list[BenchmarkProject] = []
    for p in raw["projects"]:
        queries = [
            BenchmarkQuery(
                query_id=q["query_id"],
                prompt_text=q["prompt_text"],
                category=q["category"],
            )
            for q in p["queries"]
        ]
        projects.append(
            BenchmarkProject(
                name=p["name"],
                folder=p["folder"],
                description=p.get("description", ""),
                language=p.get("language", "Java"),
                queries=queries,
            )
        )
    return projects


class BenchmarkRunner:
    def __init__(
        self,
        db: DatabaseManager,
        projects: list[BenchmarkProject],
        model: str,
        *,
        reruns: int = 3,
        modes: list[str] | None = None,
        mcp_server_url: str = "http://localhost:8080/mcp",
        mcp_tool_names: tuple[str, ...] = (),
        codebases_root: str = "codebases",
        max_iterations: int = 100,
        judge_model: str = "claude-sonnet-4-5",
        judge_max_iterations: int = 10,
        concurrency: int = 1,
    ) -> None:
        self._db = db
        self._projects = projects
        self._model = model
        self._reruns = reruns
        self._modes = modes or ["baseline", "mcp"]
        self._mcp_url = mcp_server_url
        self._mcp_tool_names = mcp_tool_names
        self._codebases_root = codebases_root
        self._max_iterations = max_iterations
        self._judge_model = judge_model
        self._judge_max_iterations = judge_max_iterations
        self._concurrency = concurrency

    def _count_total_runs(self) -> int:
        total_queries = sum(len(p.queries) for p in self._projects)
        return total_queries * len(self._modes) * self._reruns

    async def run_all(self) -> dict[str, Any]:
        total = self._count_total_runs()
        start = time.monotonic()

        log.info(
            "Starting benchmark: %d project(s), model=%s, %d mode(s), %d rerun(s) = %d total runs",
            len(self._projects),
            self._model,
            len(self._modes),
            self._reruns,
            total,
        )

        sem = asyncio.Semaphore(self._concurrency)

        async def _run_task(
            config: RunConfig,
            query: BenchmarkQuery,
            run_num: int,
            progress: int,
        ) -> bool:
            async with sem:
                return await self._execute_single(config, query, run_num, progress, total)

        tasks: list[asyncio.Task[bool] | Any] = []
        progress_idx = 0

        for project in self._projects:
            codebase_path = str(Path(self._codebases_root) / project.folder)
            log.info("=== Project: %s (folder: %s) ===", project.name, codebase_path)

            for mode in self._modes:
                is_mcp = mode == "mcp"
                config = RunConfig(
                    model_name=self._model,
                    is_mcp_enabled=is_mcp,
                    project_name=project.name,
                    project_description=project.description,
                    project_language=project.language,
                    max_iterations=self._max_iterations,
                    mcp_server_url=self._mcp_url,
                    mcp_tool_names=self._mcp_tool_names,
                    codebase_path=codebase_path,
                )

                for query in project.queries:
                    for run_num in range(1, self._reruns + 1):
                        progress_idx += 1
                        tasks.append(
                            _run_task(config, query, run_num, progress_idx)
                        )

        results = await asyncio.gather(*tasks)
        completed = len(results)
        errors = sum(1 for r in results if not r)

        elapsed = time.monotonic() - start
        summary = {
            "total_runs": total,
            "completed": completed,
            "errors": errors,
            "elapsed_seconds": round(elapsed, 1),
        }

        log.info("=" * 60)
        log.info("BENCHMARK COMPLETE")
        log.info("  Total runs:  %d", total)
        log.info("  Errors:      %d", errors)
        log.info("  Elapsed:     %.1f s", elapsed)
        log.info("=" * 60)

        return summary

    async def _execute_single(
        self,
        config: RunConfig,
        query: BenchmarkQuery,
        run_number: int,
        progress: int,
        total: int,
    ) -> bool:
        label = (
            f"[{progress}/{total}] "
            f"{config.project_name} | {config.model_name} | "
            f"{'MCP' if config.is_mcp_enabled else 'Baseline'} | "
            f"{query.query_id} | run {run_number}"
        )
        log.info("-> %s", label)

        run_start = time.monotonic()
        try:
            state = await run_agent(query, config)
        except Exception as exc:
            log.error("X CRASHED: %s", exc)
            self._db.save_run(
                project_name=config.project_name,
                model_name=config.model_name,
                query_id=query.query_id,
                query_category=query.category,
                is_mcp_enabled=config.is_mcp_enabled,
                run_number=run_number,
                total_prompt_tokens=0,
                total_completion_tokens=0,
                total_tokens=0,
                total_tool_calls=0,
                iterations=0,
                duration_seconds=time.monotonic() - run_start,
                final_answer=f"CRASH: {exc}",
                status="error",
            )
            return False

        duration = time.monotonic() - run_start

        run_id = self._db.save_run(
            project_name=config.project_name,
            model_name=config.model_name,
            query_id=query.query_id,
            query_category=query.category,
            is_mcp_enabled=config.is_mcp_enabled,
            run_number=run_number,
            total_prompt_tokens=state.cumulative_prompt_tokens,
            total_completion_tokens=state.cumulative_completion_tokens,
            total_cache_creation_tokens=state.cumulative_cache_creation_tokens,
            total_cache_read_tokens=state.cumulative_cache_read_tokens,
            total_tokens=state.total_tokens,
            total_tool_calls=state.tool_call_count,
            iterations=state.iterations,
            duration_seconds=duration,
            final_answer=state.final_answer,
            tool_calls_log=state.tool_calls_log,
            status=state.status,
        )

        log.info(
            "  %s - %d tokens (%d in + %d out, cache: %d created / %d read), "
            "%d tool calls, %d iterations, %.1fs",
            state.status,
            state.total_tokens,
            state.cumulative_prompt_tokens,
            state.cumulative_completion_tokens,
            state.cumulative_cache_creation_tokens,
            state.cumulative_cache_read_tokens,
            state.tool_call_count,
            state.iterations,
            duration,
        )

        if state.status == "completed" and state.final_answer:
            eval_result = await evaluate_answer(
                question=query.prompt_text,
                answer=state.final_answer,
                judge_model=self._judge_model,
                codebase_path=config.codebase_path,
                mcp_server_url=self._mcp_url if self._mcp_tool_names else None,
                mcp_tool_names=self._mcp_tool_names,
                max_iterations=self._judge_max_iterations,
            )
            self._db.update_score(
                run_id,
                correctness_score=eval_result["score"],
                judge_reasoning=eval_result["reasoning"],
                judge_model=eval_result["judge_model"],
            )
            log.info(
                "  Score: %s - %s",
                eval_result["score"],
                eval_result["reasoning"][:100],
            )

        return True
