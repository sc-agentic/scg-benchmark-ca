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

JUDGE_RUBRIC_RESEARCH_PROMPT = """\
You are an expert code-comprehension evaluator. You will receive:
1. A QUESTION about a software codebase.
2. An ANSWER produced by an AI agent.
3. A RUBRIC listing the must_cover elements a correct answer must contain.

Do BOTH parts independently — do not let one part influence the other.

PART 1 — Rubric coverage (use tools to verify):
For each must_cover item, decide: COVERED / PARTIAL / MISSING / WRONG, with brief evidence.

PART 2 — Independent factual spot-check (use tools to verify):
Quote 5 of the MOST SPECIFIC factual claims in the ANSWER (class types, field/method membership, signatures, ordering, default values, constants). For each: CORRECT / INCORRECT / UNVERIFIABLE + evidence. Choose claims independently of the rubric — do not just re-verify must_cover items.

When choosing claims to spot-check, hunt especially for these common error shapes:
- Wrong class membership: claiming class X has field/method Y when Y lives elsewhere.
- Inheritance vs implementation: "extends X" vs "implements X" are not interchangeable.
- Wrong type signatures: incorrect generics, return types, or argument lists.
- Wrong ordering in sequences: initialization, cache lookup, lifecycle phases.
- Hallucinations: classes/methods/fields/constants that don't exist.
- Wrong defaults: incorrect default values, enum names, or magic numbers.

Output a structured checklist:
MUST_COVER:
  - <item 1>: <verdict> — <evidence>
  ...
SPOT_CHECK (exactly 5 entries):
  - "<quoted claim>": <verdict> — <evidence>
  ...
SUMMARY: <2-3 sentences>
"""

JUDGE_RUBRIC_SCORE_PROMPT = """\
You are a scoring assistant. Based on the structured verification findings, output ONLY a JSON object — no other text.

Scoring scale (5 levels), integrating BOTH must_cover coverage AND spot-check results:
- 1.00 = All must_cover items COVERED and all spot-checked claims CORRECT (UNVERIFIABLE doesn't count against).
- 0.75 = One must_cover PARTIAL, OR one spot-checked claim INCORRECT, otherwise solid.
- 0.50 = One must_cover MISSING/WRONG, OR 2+ spot-check claims INCORRECT, OR a mix of moderate issues.
- 0.25 = Multiple must_cover MISSING/WRONG together with several spot-check errors.
- 0.00 = Empty, irrelevant, or fundamentally describes a different subject.

Both signals matter equally — a high must_cover score with multiple verified factual errors must NOT earn 1.00. Do not penalize stylistic differences, ordering of presentation, or extra detail beyond the rubric.

Respond with EXACTLY this format and nothing else:
{"score": <0.0 or 0.25 or 0.5 or 0.75 or 1.0>, "reasoning": "<brief explanation citing both must_cover verdicts and specific spot-check findings>"}
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
    research_system_prompt: str = JUDGE_RESEARCH_PROMPT,
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
        system_prompt=research_system_prompt,
        cwd=codebase_path,
        model=judge_model,
        max_turns=max_iterations,
        tools=JUDGE_BUILTIN_TOOLS,
        allowed_tools=allowed,
        mcp_servers=mcp_servers,
        permission_mode="bypassPermissions",
        cli_path=_cli_path(),
    )


def _build_scoring_options(
    *,
    judge_model: str,
    codebase_path: str,
    scoring_system_prompt: str = JUDGE_SCORE_PROMPT,
) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=scoring_system_prompt,
        cwd=codebase_path,
        model=judge_model,
        max_turns=2,
        tools=[],
        allowed_tools=[],
        permission_mode="bypassPermissions",
        cli_path=_cli_path(),
    )


_VALID_SCORES_3 = (0.0, 0.5, 1.0)
_VALID_SCORES_5 = (0.0, 0.25, 0.5, 0.75, 1.0)


def _parse_score(content: str, scale: str = "3point") -> tuple[float, str]:
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

    valid = _VALID_SCORES_5 if scale == "5point" else _VALID_SCORES_3
    score = min(valid, key=lambda v: abs(v - score))

    reasoning = result.get("reasoning", "")
    return score, reasoning


async def _collect_text(gen) -> tuple[str, int, int]:
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
    rubric: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not answer or answer.strip() in ("", "(max iterations reached)"):
        return {
            "score": 0.0,
            "reasoning": "Empty or failed answer - automatic 0.",
            "judge_model": judge_model,
            "judge_prompt_tokens": 0,
            "judge_completion_tokens": 0,
        }

    use_rubric = rubric is not None
    scale = "5point" if use_rubric else "3point"

    research_system_prompt = JUDGE_RUBRIC_RESEARCH_PROMPT if use_rubric else JUDGE_RESEARCH_PROMPT
    scoring_system_prompt = JUDGE_RUBRIC_SCORE_PROMPT if use_rubric else JUDGE_SCORE_PROMPT

    if use_rubric:
        rubric_text = json.dumps(rubric, indent=2)
        research_prompt = (
            f"QUESTION:\n{question}\n\n"
            f"ANSWER:\n{answer}\n\n"
            f"RUBRIC (per-element scoring criteria):\n{rubric_text}"
        )
    else:
        research_prompt = f"QUESTION:\n{question}\n\nANSWER:\n{answer}"

    research_options = _build_research_options(
        judge_model=judge_model,
        codebase_path=codebase_path,
        max_iterations=max_iterations,
        mcp_server_url=mcp_server_url,
        mcp_tool_names=mcp_tool_names,
        research_system_prompt=research_system_prompt,
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

    if use_rubric:
        scoring_prompt = (
            f"QUESTION:\n{question}\n\n"
            f"ANSWER:\n{answer}\n\n"
            f"RUBRIC:\n{json.dumps(rubric, indent=2)}\n\n"
            f"VERIFICATION FINDINGS:\n{research or '(no findings)'}\n\n"
            f"Apply the rubric's scoring_guide and output ONLY the JSON score object now."
        )
    else:
        scoring_prompt = (
            f"QUESTION:\n{question}\n\n"
            f"ANSWER:\n{answer}\n\n"
            f"RESEARCH FINDINGS:\n{research or '(no findings)'}\n\n"
            f"Output ONLY the JSON score object now."
        )
    scoring_options = _build_scoring_options(
        judge_model=judge_model,
        codebase_path=codebase_path,
        scoring_system_prompt=scoring_system_prompt,
    )

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
        score, reasoning = _parse_score(scoring_text, scale=scale)
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
