import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src.database import DatabaseManager
from src.judge import evaluate_answer
from src.mcp_discovery import list_mcp_tool_names

load_dotenv()

log = logging.getLogger(__name__)


DEFAULT_AGENT_MODEL = "claude-sonnet-4-6"
DEFAULT_PROJECT = "Glide"
DEFAULT_QUERIES = ("Q4", "Q8", "Q10", "Q15")
DEFAULT_CONFIGS = ("baseline", "mcp", "mcp+skill")


@dataclass(frozen=True)
class Variant:
    name: str
    judge_model: str
    use_mcp: bool
    use_rubric: bool


VARIANTS: tuple[Variant, ...] = (
    Variant("baseline_judge", "claude-sonnet-4-6", use_mcp=False, use_rubric=False),
    Variant("opus_judge",     "claude-opus-4-7",   use_mcp=False, use_rubric=False),
    Variant("mcp_judge",      "claude-sonnet-4-6", use_mcp=True,  use_rubric=False),
    Variant("rubric_judge",   "claude-sonnet-4-6", use_mcp=False, use_rubric=True),
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase A ablation experiment.")
    p.add_argument("--db", default="results.db")
    p.add_argument("--queries-json", default="queries.json")
    p.add_argument("--codebases-root", default="codebases")
    p.add_argument("--mcp-url", default="http://localhost:8080/mcp")
    p.add_argument("--agent-model", default=DEFAULT_AGENT_MODEL)
    p.add_argument("--project", default=DEFAULT_PROJECT)
    p.add_argument(
        "--queries",
        default=",".join(DEFAULT_QUERIES),
        help="Comma-separated query_ids (default: Q4,Q8,Q10,Q15)",
    )
    p.add_argument("--judge-max-iterations", type=int, default=50)
    p.add_argument(
        "--variants",
        default=",".join(v.name for v in VARIANTS),
        help=f"Comma-separated subset of: {','.join(v.name for v in VARIANTS)}",
    )
    p.add_argument(
        "--runs",
        default=None,
        help="Comma-separated list of benchmark_runs.id to restrict evaluation to (e.g. '141,150,227,237').",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan and exit without calling judge.",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=6,
        help="Max parallel judge calls (default: 6).",
    )
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p.parse_args()


def _config_name(run: dict[str, Any]) -> str:
    suffix = "+tools" if run.get("builtin_tools_enabled") else ""
    if run.get("skill_enabled"):
        return f"mcp+skill{suffix}"
    if run.get("is_mcp_enabled"):
        return f"mcp{suffix}"
    return "baseline"


def _load_prompts_and_folders(queries_path: str) -> tuple[dict[str, str], dict[str, str]]:
    data = json.loads(Path(queries_path).read_text(encoding="utf-8"))
    prompts: dict[str, str] = {}
    folders: dict[str, str] = {}
    for project in data.get("projects", []):
        folders[project["name"]] = project["folder"]
        for q in project.get("queries", []):
            prompts[q["query_id"]] = q["prompt_text"]
    return prompts, folders


def _load_rubrics(queries_path: str) -> dict[str, dict]:
    data = json.loads(Path(queries_path).read_text(encoding="utf-8"))
    rubrics: dict[str, dict] = {}
    for project in data.get("projects", []):
        for q in project.get("queries", []):
            if q.get("must_cover"):
                rubrics[q["query_id"]] = {"must_cover": q["must_cover"]}
    return rubrics


def _select_runs(
    db: DatabaseManager, *, project: str, model: str, query_ids: tuple[str, ...]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for qid in query_ids:
        for run in db.get_runs(model_name=model, query_id=qid):
            if run["project_name"] != project:
                continue
            if run["status"] != "completed":
                continue
            rows.append(run)
    return rows


async def main() -> None:
    args = parse_args()

    level = logging.WARNING
    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose >= 1:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%H:%M:%S",
    )

    query_ids = tuple(q.strip() for q in args.queries.split(",") if q.strip())
    variant_names = {v.strip() for v in args.variants.split(",") if v.strip()}
    selected_variants = tuple(v for v in VARIANTS if v.name in variant_names)
    if not selected_variants:
        print(f"ERROR: no variants matched {variant_names}", file=sys.stderr)
        sys.exit(2)

    prompts, folders = _load_prompts_and_folders(args.queries_json)
    rubrics = _load_rubrics(args.queries_json)

    if any(v.use_rubric for v in selected_variants):
        missing = [q for q in query_ids if q not in rubrics]
        if missing:
            print(f"ERROR: missing rubrics for {missing}", file=sys.stderr)
            sys.exit(2)

    mcp_tool_names: tuple[str, ...] = ()
    if any(v.use_mcp for v in selected_variants):
        try:
            mcp_tool_names = tuple(await list_mcp_tool_names(args.mcp_url))
        except Exception as exc:
            print(
                f"ERROR: MCP server unreachable at {args.mcp_url}: {exc}",
                file=sys.stderr,
            )
            print(
                "Start the MCP server, or omit mcp_judge from --variants to skip MCP.",
                file=sys.stderr,
            )
            sys.exit(1)

    allowed_ids: set[int] | None = None
    if args.runs:
        allowed_ids = {int(r.strip()) for r in args.runs.split(",") if r.strip()}

    with DatabaseManager(args.db) as db:
        runs = _select_runs(
            db, project=args.project, model=args.agent_model, query_ids=query_ids
        )
        if allowed_ids is not None:
            runs = [r for r in runs if r["id"] in allowed_ids]

        print(
            f"Found {len(runs)} agent runs in scope "
            f"(project={args.project}, model={args.agent_model}, "
            f"queries={','.join(query_ids)})."
        )
        per_config: dict[str, int] = {}
        for r in runs:
            per_config[_config_name(r)] = per_config.get(_config_name(r), 0) + 1
        print(f"  Per config: {per_config}")
        print(f"  Variants:   {[v.name for v in selected_variants]}")
        total_planned = sum(
            1
            for v in selected_variants
            for r in runs
            if r["id"] not in db.get_ablation_done(v.name)
        )
        print(f"  Pending judge calls (after dedup): {total_planned}\n")

        if args.dry_run:
            print("Dry run; exiting.")
            return

        pending: list[tuple[Variant, dict[str, Any]]] = []
        skipped = 0
        for variant in selected_variants:
            done = db.get_ablation_done(variant.name)
            for run in runs:
                if run["id"] in done:
                    skipped += 1
                    continue
                pending.append((variant, run))

        total = len(pending)
        print(
            f"\nScheduling {total} judge calls across {len(selected_variants)} variant(s) "
            f"with concurrency={args.concurrency}.  (Skipped {skipped} already done.)\n"
        )

        sem = asyncio.Semaphore(args.concurrency)
        progress = {"done": 0, "ok": 0, "fail": 0}
        start = time.monotonic()

        async def judge_one(variant: Variant, run: dict[str, Any]) -> None:
            run_id = run["id"]
            qid = run["query_id"]
            config = _config_name(run)
            question = prompts.get(qid, qid)
            rubric = rubrics.get(qid) if variant.use_rubric else None
            folder = folders.get(run["project_name"], run["project_name"])
            codebase_path = str(Path(args.codebases_root) / folder)
            answer = run.get("final_answer") or ""
            mcp_url = args.mcp_url if variant.use_mcp else None
            mcp_tools = mcp_tool_names if variant.use_mcp else ()

            async with sem:
                t0 = time.monotonic()

                if not answer.strip():
                    db.save_ablation_result(
                        source_run_id=run_id,
                        variant_name=variant.name,
                        judge_model=variant.judge_model,
                        judge_has_mcp=variant.use_mcp,
                        uses_rubric=variant.use_rubric,
                        score=0.0,
                        reasoning="Empty answer - automatic 0.",
                    )
                    score_val: float = 0.0
                    reasoning = "empty answer"
                else:
                    try:
                        result = await evaluate_answer(
                            question=question,
                            answer=answer,
                            judge_model=variant.judge_model,
                            codebase_path=codebase_path,
                            mcp_server_url=mcp_url,
                            mcp_tool_names=mcp_tools,
                            max_iterations=args.judge_max_iterations,
                            rubric=rubric,
                        )
                        score_val = result["score"]
                        reasoning = result["reasoning"]
                        db.save_ablation_result(
                            source_run_id=run_id,
                            variant_name=variant.name,
                            judge_model=result["judge_model"],
                            judge_has_mcp=variant.use_mcp,
                            uses_rubric=variant.use_rubric,
                            score=score_val,
                            reasoning=reasoning,
                            judge_prompt_tokens=result.get("judge_prompt_tokens", 0),
                            judge_completion_tokens=result.get("judge_completion_tokens", 0),
                        )
                    except Exception as exc:
                        log.error("evaluate_answer crashed for run %d: %s", run_id, exc)
                        db.save_ablation_result(
                            source_run_id=run_id,
                            variant_name=variant.name,
                            judge_model=variant.judge_model,
                            judge_has_mcp=variant.use_mcp,
                            uses_rubric=variant.use_rubric,
                            score=-1.0,
                            reasoning=f"crash: {exc}",
                        )
                        score_val = -1.0
                        reasoning = f"crash: {exc}"

                progress["done"] += 1
                if score_val >= 0:
                    progress["ok"] += 1
                else:
                    progress["fail"] += 1

                dt = time.monotonic() - t0
                print(
                    f"  [{progress['done']:>3}/{total}] {variant.name:<14} "
                    f"run={run_id} {qid:<4} | {config:<9} | "
                    f"{score_val:>5.2f}  ({dt:>5.1f}s)  {reasoning[:80]}"
                )

        await asyncio.gather(*(judge_one(v, r) for v, r in pending))

        elapsed = time.monotonic() - start
        print(
            f"\nDone. ok={progress['ok']}  fail={progress['fail']}  "
            f"skipped={skipped}  ({elapsed/60:.1f} min)"
        )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
