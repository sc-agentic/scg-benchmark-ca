from dataclasses import dataclass, field
from typing import Any


@dataclass
class BenchmarkQuery:
    query_id: str
    prompt_text: str
    category: str


@dataclass
class BenchmarkProject:
    name: str
    folder: str
    description: str
    language: str
    queries: list[BenchmarkQuery]


@dataclass
class AgentState:
    cumulative_prompt_tokens: int = 0
    cumulative_completion_tokens: int = 0
    cumulative_cache_creation_tokens: int = 0
    cumulative_cache_read_tokens: int = 0
    tool_call_count: int = 0
    tool_calls_log: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    final_answer: str | None = None
    status: str = "running"  # running | completed | error | max_iterations

    @property
    def total_tokens(self) -> int:
        return self.cumulative_prompt_tokens + self.cumulative_completion_tokens

    def record_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        self.cumulative_prompt_tokens += prompt_tokens
        self.cumulative_completion_tokens += completion_tokens
        self.cumulative_cache_creation_tokens += cache_creation_tokens
        self.cumulative_cache_read_tokens += cache_read_tokens

    def record_tool_call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        result_length: int,
        error: str | None = None,
    ) -> None:
        self.tool_call_count += 1
        self.tool_calls_log.append(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "result_length": result_length,
                "error": error,
            }
        )


@dataclass
class RunConfig:
    model_name: str
    is_mcp_enabled: bool
    project_name: str = ""
    project_description: str = ""
    project_language: str = "Java"
    max_iterations: int = 100
    mcp_server_url: str = "http://localhost:8080/mcp"
    mcp_tool_names: tuple[str, ...] = ()
    codebase_path: str = "codebases/glide"
