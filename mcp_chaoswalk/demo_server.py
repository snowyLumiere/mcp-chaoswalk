"""Read-only demo with fixed records and an optional finite delay."""

import asyncio
import shlex
import sys
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

DEMO_COMMAND = shlex.join([sys.executable, "-B", str(Path(__file__).resolve())])
DEMO_DEFAULT_TOOLS = ("list_examples", "get_example")
DEMO_TOOL_NAMES = frozenset((*DEMO_DEFAULT_TOOLS, "get_slow_example"))

server = FastMCP("chaoswalk-demo")
read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


@server.tool(annotations=read_only)
def list_examples() -> dict:
    """List the fixture's example records."""
    return {"example_ids": ["example-1", "example-2"]}


@server.tool(annotations=read_only)
def get_example(example_id: Literal["example-1", "example-2"]) -> dict:
    """Read an example record from the fixture."""
    return {"example_id": example_id, "status": "ready"}


@server.tool(annotations=read_only)
async def get_slow_example() -> dict:
    """Read a record after a predictable half-second delay."""
    await asyncio.sleep(0.5)
    return {"example_id": "example-1", "status": "ready"}


if __name__ == "__main__":
    server.run(transport="stdio")
