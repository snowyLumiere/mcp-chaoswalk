"""Mock Filesystem MCP Server.

Uses an in-memory dictionary to model file operations and lock state.
"""

import asyncio
import json
from typing import Dict

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

server = FastMCP("mock-filesystem")

_fs_storage: Dict[str, str] = {}
_file_locks: Dict[str, str] = {}


@server.tool()
def write_file(path: str, content: str = "default content") -> str:
    """Create or overwrite a file."""
    _fs_storage[path] = content
    return json.dumps({"path": path, "size": len(content), "status": "written"})


@server.tool()
def lock_file(path: str, lock_holder: str = "agent-1") -> str:
    """Acquire exclusive lock on a file."""
    if path not in _fs_storage:
        return json.dumps({"error": f"File {path} not found", "is_error": True})
    _file_locks[path] = lock_holder
    return json.dumps({"path": path, "locked_by": lock_holder, "status": "locked"})


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
async def read_file(path: str) -> str:
    """Read contents of a file.
    
    A specific lock holder adds a finite response delay.
    """
    if _file_locks.get(path) == "agent-deadlock":
        await asyncio.sleep(6.0)
        return json.dumps({"path": path, "content": "deadlock_recovery"})

    if path not in _fs_storage:
        raise ToolError(f"File {path} not found")
    return json.dumps({"path": path, "content": _fs_storage[path]})


@server.tool()
def delete_file(path: str) -> str:
    """Delete a file from the filesystem.
    
    BUG: Deletes storage without checking or releasing _file_locks[path]!
    """
    if path in _fs_storage:
        del _fs_storage[path]
        return json.dumps({"path": path, "status": "deleted"})
    return json.dumps({"error": f"File {path} not found", "is_error": True})


@server.tool()
def unlock_file(path: str) -> str:
    """Release file lock.
    
    CRASH BUG: Asserts file exists in _fs_storage when releasing lock.
    If delete_file was called first, crashes with unhandled KeyError!
    """
    if path not in _file_locks:
        return json.dumps({"error": f"No lock on {path}", "is_error": True})
    
    # UNHANDLED EXCEPTION BUG:
    content = _fs_storage[path]  # KeyError if deleted!
    del _file_locks[path]
    return json.dumps({"path": path, "status": "unlocked", "last_content_len": len(content)})


if __name__ == "__main__":
    server.run(transport="stdio")
