import argparse
import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from src.database import DatabaseManager
from src.mcp_discovery import list_mcp_tool_names
from src.runner import BenchmarkRunner, load_projects

load_dotenv()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="SCG Benchmarking Pipeline - compare baseline grep vs MCP graph tools (Claude Agent SDK)",
    )
    p.add_argument(
        "--queries",
        default="queries.json",
        help="Path to benchmark queries JSON (default: queries.json)",
    )
    p.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        help="Claude model name (default: claude-sonnet-4-6)",
    )
    p.add_argument(
        "--reruns",
        type=int,
        default=3,
        help="Number of reruns per combination (default: 3)",
    )
    p.add_argument(
        "--db",
        default="results.db",
        help="SQLite database path (default: results.db)",
    )
    p.add_argument(
        "--mode",
        choices=["baseline", "mcp", "skill", "both", "all"],
        default="all",
        help=(
            "Which agent mode(s) to run. "
            "baseline=Read/Grep/Glob; mcp=MCP tools only; skill=MCP tools + SKILL.md "
            "guidance; both=baseline+mcp (legacy); all=baseline+mcp+skill (default)."
        ),
    )
    p.add_argument(
        "--mcp-url",
        default="http://localhost:8080/mcp",
        help="MCP server URL (default: http://localhost:8080/mcp)",
    )
    p.add_argument(
        "--skill-path",
        default="skills/scg-navigator/SKILL.md",
        help=(
            "Path to the SKILL.md whose body is injected into the system prompt "
            "in skill mode (default: skills/scg-navigator/SKILL.md)."
        ),
    )
    p.add_argument(
        "--codebases-root",
        default="codebases",
        help="Root directory containing project codebases (default: codebases)",
    )
    p.add_argument(
        "--max-iterations",
        type=int,
        default=100,
        help="Max agent turns (default: 100)",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Max concurrent agent runs (default: 1)",
    )
    p.add_argument(
        "--judge-model",
        default="claude-sonnet-4-6",
        help="Claude model used as LLM judge for scoring (default: claude-sonnet-4-6)",
    )
    p.add_argument(
        "--judge-max-iterations",
        type=int,
        default=50,
        help="Max iterations for the judge agent (default: 50)",
    )
    p.add_argument(
        "--builtin-tools-with-mcp",
        action="store_true",
        help=(
            "When set, MCP/skill agents also get Read/Grep/Glob alongside MCP tools "
            "(experimental config 'mcp+skill+tools')."
        ),
    )
    p.add_argument(
        "--export-csv",
        default=None,
        help="Export all runs to CSV and exit",
    )
    p.add_argument(
        "--projects",
        default=None,
        help="Comma-separated project names to run (default: all)",
    )
    p.add_argument(
        "--query-ids",
        default=None,
        help="Comma-separated query IDs to run (default: all)",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v for INFO, -vv for DEBUG)",
    )
    return p.parse_args()


def main() -> None:
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

    if args.export_csv:
        with DatabaseManager(args.db) as db:
            count = db.export_csv(args.export_csv)
            print(f"Exported {count} rows to {args.export_csv}")
        return

    projects = load_projects(args.queries)

    if args.projects:
        selected = set(args.projects.split(","))
        projects = [p for p in projects if p.name in selected]
        if not projects:
            print(f"ERROR: No projects matched names: {args.projects}", file=sys.stderr)
            sys.exit(1)

    if args.query_ids:
        selected_ids = set(args.query_ids.split(","))
        for project in projects:
            project.queries = [q for q in project.queries if q.query_id in selected_ids]
        projects = [p for p in projects if p.queries]
        if not projects:
            print(f"ERROR: No queries matched IDs: {args.query_ids}", file=sys.stderr)
            sys.exit(1)

    modes: list[str] = []
    if args.mode in ("baseline", "both", "all"):
        modes.append("baseline")
    if args.mode in ("mcp", "both", "all"):
        modes.append("mcp")
    if args.mode in ("skill", "all"):
        modes.append("skill")

    skill_path: str | None = None
    if "skill" in modes:
        candidate = Path(args.skill_path)
        if not candidate.is_file():
            print(
                f"ERROR: --mode includes 'skill' but SKILL.md was not found at "
                f"{candidate}. Pass --skill-path or drop 'skill' from --mode.",
                file=sys.stderr,
            )
            sys.exit(1)
        skill_path = str(candidate)

    mcp_tool_names: tuple[str, ...] = ()
    if "mcp" in modes or "skill" in modes:
        try:
            names = asyncio.run(list_mcp_tool_names(args.mcp_url))
        except Exception as exc:
            print(
                f"ERROR: MCP server is not reachable at {args.mcp_url}",
                file=sys.stderr,
            )
            print(f"  Reason: {type(exc).__name__}: {exc}", file=sys.stderr)
            print(
                f"  Start the MCP server before running with --mode {args.mode}, "
                f"or use --mode baseline.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not names:
            print(
                f"ERROR: MCP server at {args.mcp_url} reported zero tools. "
                f"Refusing to run an MCP benchmark with no graph tools available.",
                file=sys.stderr,
            )
            sys.exit(1)
        mcp_tool_names = tuple(names)

    total_queries = sum(len(p.queries) for p in projects)
    total_runs = total_queries * len(modes) * args.reruns

    print("Benchmark configuration:")
    for p in projects:
        print(f"  Project:    {p.name} ({p.folder}/) - {len(p.queries)} queries")
    print(f"  Model:      {args.model}")
    print(f"  Modes:      {modes}")
    print(f"  Reruns:     {args.reruns}")
    print(f"  Total runs: {total_runs}")
    print(f"  Judge:      {args.judge_model} (max {args.judge_max_iterations} iter)")
    print(f"  Database:   {args.db}")
    if mcp_tool_names:
        print(f"  MCP tools:  {len(mcp_tool_names)} discovered at {args.mcp_url}")
    if skill_path:
        print(f"  Skill:      {skill_path}")
    print()

    with DatabaseManager(args.db) as db:
        runner = BenchmarkRunner(
            db=db,
            projects=projects,
            model=args.model,
            reruns=args.reruns,
            modes=modes,
            mcp_server_url=args.mcp_url,
            mcp_tool_names=mcp_tool_names,
            codebases_root=args.codebases_root,
            max_iterations=args.max_iterations,
            judge_model=args.judge_model,
            judge_max_iterations=args.judge_max_iterations,
            concurrency=args.concurrency,
            skill_path=skill_path,
            builtin_tools_with_mcp=args.builtin_tools_with_mcp,
        )
        summary = asyncio.run(runner.run_all())

    print(f"\nDone. Results saved to {args.db}")
    if summary["errors"]:
        print(f"NOTICE:  {summary['errors']} runs encountered errors.")


if __name__ == "__main__":
    main()
