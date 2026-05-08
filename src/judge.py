import json
import logging
import os
import re
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

log = logging.getLogger(__name__)

JUDGE_RESEARCH_PROMPT = """\
You are an expert code-comprehension evaluator. You will receive:
1. A QUESTION about a software codebase.
2. An ANSWER produced by an AI agent.

Use your provided tools to search the codebase and verify the factual correctness of the ANSWER.
Investigate thoroughly, then write a brief summary of what you found and whether the answer is correct.
"""

JUDGE_SCORE_PROMPT = """\
You are a scoring assistant. Based on the provided research findings, output ONLY a JSON object — no other text.

Scoring scale:
- 1.0 = Correct — accurate, complete, demonstrates genuine understanding.
- 0.5 = Partial — partially correct but has notable gaps or inaccuracies.
- 0.0 = Wrong — fundamentally incorrect, irrelevant, or essentially empty.

Respond with EXACTLY this format and nothing else:
{"score": <0.0 or 0.5 or 1.0>, "reasoning": "<brief explanation>"}
"""

JUDGE_BUILTIN_TOOLS = ["Read", "Grep", "Glob"]
MCP_SERVER_NAME = "scg"


def _cli_path() -> str | None:
    return os.environ.get("CLAUDE_CLI_PATH") or None


def _build_research_options(
    *,
    judge_model: str,
    codebase_path: str,
    max_iterations: int,
    mcp_server_url: str | None,
    mcp_tool_names: tuple[str, ...] | list[str],
) -> ClaudeAgentOptions:
    allowed = list(JUDGE_BUILTIN_TOOLS)
    mcp_servers: dict[str, Any] = {}

    if mcp_server_url and mcp_tool_names:
        allowed.extend(mcp_tool_names)
        mcp_servers[MCP_SERVER_NAME] = {
            "type": "http",
            "url": mcp_server_url,
        }

    return ClaudeAgentOptions(
        system_prompt=JUDGE_RESEARCH_PROMPT,
        cwd=codebase_path,
        model=judge_model,
        max_turns=max_iterations,
        tools=JUDGE_BUILTIN_TOOLS,
        allowed_tools=allowed,
        mcp_servers=mcp_servers,
        permission_mode="bypassPermissions",
        cli_path=_cli_path(),
    )


def _build_scoring_options(*, judge_model: str, codebase_path: str) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=JUDGE_SCORE_PROMPT,
        cwd=codebase_path,
        model=judge_model,
        max_turns=2,
        tools=[],
        allowed_tools=[],
        permission_mode="bypassPermissions",
        cli_path=_cli_path(),
    )


def _parse_score(content: str) -> tuple[float, str]:
    cleaned = content.strip()

    json_match = re.search(r'\{[^{}]*"score"[^{}]*\}', cleaned, re.DOTALL)
    if json_match:
        cleaned = json_match.group(0)
    elif cleaned.startswith("```"):
        lines = cleaned.split("\n", 1)
        if len(lines) > 1:
            cleaned = lines[1].rsplit("```", 1)[0].strip()

    result = json.loads(cleaned)
    score = float(result["score"])

    if score >= 0.75:
        score = 1.0
    elif score >= 0.25:
        score = 0.5
    else:
        score = 0.0

    reasoning = result.get("reasoning", "")
    return score, reasoning


async def _collect_text(gen) -> tuple[str, int, int]:
    """Drain an SDK query generator, returning (joined_text, input_tokens, output_tokens).

    Breaks immediately on ResultMessage. The bundled claude CLI exits with
    code 1 on error_max_turns; if we keep iterating past ResultMessage the SDK
    raises ProcessError when the subprocess exits non-zero — but we already
    have everything we need from ResultMessage.
    """
    blocks: list[str] = []
    in_tok = out_tok = 0
    async for msg in gen:
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    blocks.append(block.text)
        elif isinstance(msg, ResultMessage):
            usage = msg.usage or {}
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            if msg.is_error and msg.subtype == "error_max_turns":
                log.warning(
                    "Judge hit max_turns (%d turns). Research may be incomplete.",
                    msg.num_turns,
                )
            break
    return "\n".join(blocks).strip(), in_tok, out_tok


async def evaluate_answer(
    *,
    question: str,
    answer: str,
    judge_model: str,
    codebase_path: str,
    mcp_server_url: str | None = None,
    mcp_tool_names: tuple[str, ...] | list[str] = (),
    max_iterations: int = 10,
) -> dict[str, Any]:
    if not answer or answer.strip() in ("", "(max iterations reached)"):
        return {
            "score": 0.0,
            "reasoning": "Empty or failed answer - automatic 0.",
            "judge_model": judge_model,
            "judge_prompt_tokens": 0,
            "judge_completion_tokens": 0,
        }

    research_prompt = f"QUESTION:\n{question}\n\nANSWER:\n{answer}"
    research_options = _build_research_options(
        judge_model=judge_model,
        codebase_path=codebase_path,
        max_iterations=max_iterations,
        mcp_server_url=mcp_server_url,
        mcp_tool_names=mcp_tool_names,
    )

    judge_prompt_tokens = 0
    judge_completion_tokens = 0

    try:
        research, in_tok, out_tok = await _collect_text(
            query(prompt=research_prompt, options=research_options)
        )
        judge_prompt_tokens += in_tok
        judge_completion_tokens += out_tok
    except Exception as exc:
        log.error("Judge research phase failed: %s", exc)
        return {
            "score": -1.0,
            "reasoning": f"Judge SDK error: {exc}",
            "judge_model": judge_model,
            "judge_prompt_tokens": judge_prompt_tokens,
            "judge_completion_tokens": judge_completion_tokens,
        }

    # Phase 2: single-turn scoring call — no tools, forces JSON output.
    scoring_prompt = (
        f"QUESTION:\n{question}\n\n"
        f"ANSWER:\n{answer}\n\n"
        f"RESEARCH FINDINGS:\n{research or '(no findings)'}\n\n"
        f"Output ONLY the JSON score object now."
    )
    scoring_options = _build_scoring_options(judge_model=judge_model, codebase_path=codebase_path)

    try:
        scoring_text, in_tok, out_tok = await _collect_text(
            query(prompt=scoring_prompt, options=scoring_options)
        )
        judge_prompt_tokens += in_tok
        judge_completion_tokens += out_tok
    except Exception as exc:
        log.error("Judge scoring phase failed: %s", exc)
        return {
            "score": -1.0,
            "reasoning": f"Judge scoring error: {exc}",
            "judge_model": judge_model,
            "judge_prompt_tokens": judge_prompt_tokens,
            "judge_completion_tokens": judge_completion_tokens,
        }

    if not scoring_text:
        log.warning("Judge scoring phase returned empty content.")
        return {
            "score": -1.0,
            "reasoning": "Judge returned empty scoring response.",
            "judge_model": judge_model,
            "judge_prompt_tokens": judge_prompt_tokens,
            "judge_completion_tokens": judge_completion_tokens,
        }

    try:
        score, reasoning = _parse_score(scoring_text)
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        log.warning("Could not parse judge score: %s - raw: %r", exc, scoring_text[:300])
        score = -1.0
        reasoning = f"Parse error: {scoring_text[:300]}"

    return {
        "score": score,
        "reasoning": reasoning,
        "judge_model": judge_model,
        "judge_prompt_tokens": judge_prompt_tokens,
        "judge_completion_tokens": judge_completion_tokens,
    }
