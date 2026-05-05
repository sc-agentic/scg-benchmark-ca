import logging

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

log = logging.getLogger(__name__)


class MCPUnreachableError(RuntimeError):
    """Raised when the MCP server cannot be contacted or fails to list tools."""


def _root_cause(exc: BaseException) -> BaseException:
    """Walk through ExceptionGroups to find the underlying error."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


async def list_mcp_tool_names(
    server_url: str,
    server_name: str = "scg",
) -> list[str]:
    log.info("Discovering MCP tools at %s ...", server_url)
    try:
        async with streamablehttp_client(server_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                names = [f"mcp__{server_name}__{t.name}" for t in result.tools]
    except BaseException as exc:
        cause = _root_cause(exc)
        raise MCPUnreachableError(f"{type(cause).__name__}: {cause}") from cause

    log.info("Discovered %d MCP tool(s): %s", len(names), names)
    return names
