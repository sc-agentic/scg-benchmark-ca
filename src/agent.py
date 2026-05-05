import logging
import time

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from .models import AgentState, BenchmarkQuery, RunConfig

log = logging.getLogger(__name__)

BASELINE_TOOLS = ["Read", "Grep", "Glob"]
MCP_SERVER_NAME = "scg"


def _build_system_prompt(config: RunConfig) -> str:
    base = (
        f"You are a senior {config.project_language} developer and expert on the "
        f"{config.project_name} project.\n"
        f"Project description: {config.project_description}\n\n"
        "You have access to tools that let you explore the codebase.\n"
        "Use them to answer the user's question thoroughly and accurately.\n\n"
    )

    if config.is_mcp_enabled:
        strategy = (
            "Strategy:\n"
            "1. Identify the key classes / methods relevant to the question.\n"
            "2. Explore their relationships, dependencies, and hierarchies using the graph.\n"
            "3. Read source code only when you need implementation details.\n"
            "4. Synthesize your findings into a clear, structured answer.\n\n"
        )
    else:
        strategy = (
            "Strategy:\n"
            "1. Search for key terms, class names, or function names related to the question.\n"
            "2. Read the files where these terms are defined or used to understand the implementation.\n"
            "3. Follow imports or function calls by further searching if necessary.\n"
            "4. Synthesize your findings into a clear, structured answer.\n\n"
        )

    closing = (
        "When you have gathered enough information, provide your final answer directly\n"
        "without calling any more tools."
    )

    return base + strategy + closing


def _build_options(config: RunConfig) -> ClaudeAgentOptions:
    system_prompt = _build_system_prompt(config)

    if config.is_mcp_enabled:
        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            cwd=config.codebase_path,
            model=config.model_name,
            max_turns=config.max_iterations,
            mcp_servers={
                MCP_SERVER_NAME: {
                    "type": "http",
                    "url": config.mcp_server_url,
                }
            },
            allowed_tools=list(config.mcp_tool_names),
            permission_mode="bypassPermissions",
        )

    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        cwd=config.codebase_path,
        model=config.model_name,
        max_turns=config.max_iterations,
        allowed_tools=BASELINE_TOOLS,
        permission_mode="bypassPermissions",
    )


def _stringify_tool_result(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
            elif isinstance(block, dict) and "text" in block:
                parts.append(block["text"])
        return "\n".join(parts)
    return str(content)


async def run_agent(
    benchmark_query: BenchmarkQuery,
    config: RunConfig,
) -> AgentState:
    state = AgentState()
    options = _build_options(config)

    pending_tool_calls: dict[str, dict] = {}
    text_blocks: list[str] = []

    start = time.monotonic()

    try:
        async for msg in query(prompt=benchmark_query.prompt_text, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        pending_tool_calls[block.id] = {
                            "tool_name": block.name,
                            "arguments": dict(block.input or {}),
                        }
                    elif isinstance(block, TextBlock):
                        text_blocks.append(block.text)

            elif isinstance(msg, UserMessage):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        info = pending_tool_calls.pop(block.tool_use_id, None)
                        if info is None:
                            continue
                        result_str = _stringify_tool_result(block.content)
                        state.record_tool_call(
                            tool_name=info["tool_name"],
                            arguments=info["arguments"],
                            result_length=len(result_str),
                            error=result_str if block.is_error else None,
                        )

            elif isinstance(msg, ResultMessage):
                usage = msg.usage or {}
                state.record_usage(
                    prompt_tokens=usage.get("input_tokens", 0),
                    completion_tokens=usage.get("output_tokens", 0),
                    cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                    cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                )
                state.iterations = msg.num_turns
                state.final_answer = (
                    msg.result if msg.result else "\n".join(text_blocks).strip() or None
                )
                if msg.is_error:
                    state.status = (
                        "max_iterations"
                        if msg.subtype == "error_max_turns"
                        else "error"
                    )
                else:
                    state.status = "completed"
                break
    except Exception as exc:
        log.error("Agent run failed: %s", exc)
        state.status = "error"
        if state.final_answer is None:
            state.final_answer = f"SDK error: {exc}"

    elapsed = time.monotonic() - start
    log.info(
        "Run complete - status=%s, iterations=%d, tokens=%d, tool_calls=%d, %.1fs",
        state.status,
        state.iterations,
        state.total_tokens,
        state.tool_call_count,
        elapsed,
    )
    return state
