import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.database import DatabaseManager
from src.judge import evaluate_answer
from src.mcp_discovery import list_mcp_tool_names

load_dotenv()

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Re-evaluate failed/missing judge scores")
    p.add_argument("--db", default="results.db")
    p.add_argument("--queries", default="queries.json", help="Path to queries JSON")
    p.add_argument("--judge-model", default="claude-sonnet-4-5")
    p.add_argument("--judge-max-iterations", type=int, default=30)
    p.add_argument("--mcp-url", default="http://localhost:8080/mcp")
    p.add_argument("--codebases-root", default="codebases")
    p.add_argument(
        "--no-mcp",
        action="store_true",
        help="Skip MCP tool discovery; judge uses built-in tools only",
    )
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p.parse_args()


def _load_prompt_map(queries_path: str) -> dict[str, str]:
    data = json.loads(Path(queries_path).read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    for project in data.get("projects", []):
        for q in project.get("queries", []):
            mapping[q["query_id"]] = q["prompt_text"]
    return mapping


def _load_folder_map(queries_path: str) -> dict[str, str]:
    data = json.loads(Path(queries_path).read_text(encoding="utf-8"))
    return {p["name"]: p["folder"] for p in data.get("projects", [])}


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

    prompt_map = _load_prompt_map(args.queries)
    folder_map = _load_folder_map(args.queries)

    mcp_tool_names: tuple[str, ...] = ()
    if not args.no_mcp:
        try:
            names = await list_mcp_tool_names(args.mcp_url)
        except Exception as exc:
            print(
                f"ERROR: MCP server is not reachable at {args.mcp_url}",
                file=sys.stderr,
            )
            print(f"  Reason: {type(exc).__name__}: {exc}", file=sys.stderr)
            print(
                "  Start the MCP server, or pass --no-mcp to re-judge with built-in tools only.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not names:
            print(
                f"ERROR: MCP server at {args.mcp_url} reported zero tools. "
                "Pass --no-mcp to re-judge without MCP.",
                file=sys.stderr,
            )
            sys.exit(1)
        mcp_tool_names = tuple(names)

    with DatabaseManager(args.db) as db:
        runs = db.get_unscored_runs(include_failed=True)
        if not runs:
            print("Nothing to re-evaluate - all scores are present.")
            return

        print(f"Found {len(runs)} run(s) to re-evaluate.")
        print(f"Judge: {args.judge_model}  (max {args.judge_max_iterations} iter)\n")

        ok = 0
        fail = 0

        for i, run in enumerate(runs, 1):
            run_id = run["id"]
            query_id = run["query_id"]
            project = run["project_name"]
            mode = "MCP" if run["is_mcp_enabled"] else "Baseline"
            answer = run.get("final_answer") or ""

            print(f"  [{i}/{len(runs)}] id={run_id}  {project} | {mode} | {query_id}")

            if not answer.strip():
                db.update_score(
                    run_id,
                    correctness_score=0.0,
                    judge_reasoning="Empty answer - automatic 0.",
                    judge_model=args.judge_model,
                )
                print("    -> 0.0 (empty answer)")
                ok += 1
                continue

            folder = folder_map.get(project, project)
            codebase_path = str(Path(args.codebases_root) / folder)

            question = prompt_map.get(query_id, query_id)

            try:
                result = await evaluate_answer(
                    question=question,
                    answer=answer,
                    judge_model=args.judge_model,
                    codebase_path=codebase_path,
                    mcp_server_url=args.mcp_url if mcp_tool_names else None,
                    mcp_tool_names=mcp_tool_names,
                    max_iterations=args.judge_max_iterations,
                )
            except Exception as exc:
                log.error("evaluate_answer crashed for run %d: %s", run_id, exc)
                fail += 1
                continue

            db.update_score(
                run_id,
                correctness_score=result["score"],
                judge_reasoning=result["reasoning"],
                judge_model=result["judge_model"],
            )
            status = "OK" if result["score"] >= 0 else "still -1"
            print(f"    -> {result['score']}  ({status})  {result['reasoning'][:80]}")
            if result["score"] >= 0:
                ok += 1
            else:
                fail += 1

        print(f"\nDone. {ok} scored, {fail} still failed.")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
