"""MCP client adapter for a stdio subprocess and session."""

import asyncio
import json
import os
import shlex
import time
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

from jsonschema import SchemaError, ValidationError, validate
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import McpError
from referencing import Registry
from referencing.exceptions import Unresolvable

from mcp_chaoswalk.models import CallOutcome, CallStep


class MCPStartupError(RuntimeError):
    """A startup phase failed before a tool could be called."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class MCPToolCatalogChanged(RuntimeError):
    """The server invalidated the tools approved for this session."""

    reason = "Tool catalog changed during the stdio session."


class MCPProcessClient:
    """Manage the stdio and session contexts in the same task."""

    def __init__(self, command_str: str, env: Optional[Dict[str, str]] = None):
        parts = shlex.split(command_str, posix=(os.name != "nt"))
        if not parts:
            raise ValueError("Command string cannot be empty")
        self.params = StdioServerParameters(command=parts[0], args=parts[1:], env=env)
        self._session: Optional[ClientSession] = None
        self._exit_stack: Optional[AsyncExitStack] = None
        self._output_schemas: Dict[str, Optional[Dict[str, Any]]] = {}
        self.catalog_changed = False
        self.close_error: Optional[Exception] = None

    @property
    def is_open(self) -> bool:
        return self._session is not None

    async def _handle_message(self, message: object) -> None:
        if isinstance(message, types.ServerNotification) and isinstance(message.root, types.ToolListChangedNotification):
            self.catalog_changed = True

    async def start(self, timeout_seconds: float = 5.0) -> List[types.Tool]:
        """Start the subprocess and require a complete, single-page catalog."""
        if self._exit_stack is not None:
            raise RuntimeError("MCP client already started")
        self.catalog_changed = False
        self._output_schemas.clear()
        self.close_error = None
        stack = self._exit_stack = AsyncExitStack()
        phase = "stdio launch"
        try:
            # The SDK starts with its platform environment allowlist and adds
            # only overrides explicitly supplied by the library caller.
            errlog = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
            streams = await stack.enter_async_context(stdio_client(self.params, errlog=errlog))
            self._session = await stack.enter_async_context(
                ClientSession(*streams, message_handler=self._handle_message)
            )
            phase = "MCP initialize"
            await asyncio.wait_for(self._session.initialize(), timeout=timeout_seconds)
            phase = "MCP tools/list"
            catalog = await asyncio.wait_for(self._session.list_tools(), timeout=timeout_seconds)
            if catalog.nextCursor is not None:
                raise MCPStartupError("Tool catalog incomplete (pagination unsupported)")
            if self.catalog_changed:
                raise MCPStartupError(MCPToolCatalogChanged.reason)
            self._output_schemas = {tool.name: tool.outputSchema for tool in catalog.tools}
            return catalog.tools
        except BaseException as exc:
            await self.stop()
            if isinstance(exc, (MCPStartupError, asyncio.CancelledError)):
                raise
            if isinstance(exc, asyncio.TimeoutError):
                raise MCPStartupError(f"{phase} timed out") from exc
            if isinstance(exc, Exception):
                raise MCPStartupError(f"{phase} failed") from exc
            raise

    async def call_tool_guarded(
        self,
        step_id: int,
        tool_name: str,
        arguments: Dict[str, Any],
        timeout_seconds: float = 5.0,
    ) -> CallStep:
        """Make one tool call and classify its protocol or client outcome."""
        session = self._session
        if session is None:
            raise RuntimeError("MCP client is not started")

        async def request_tool() -> types.CallToolResult:
            # Check inside the scheduled request in case a notification arrived
            # after the walker selected the tool.
            if self.catalog_changed:
                raise MCPToolCatalogChanged(MCPToolCatalogChanged.reason)
            request = types.CallToolRequest(params=types.CallToolRequestParams(name=tool_name, arguments=arguments))
            return await session.send_request(types.ClientRequest(request), types.CallToolResult)

        start_time = time.perf_counter()
        step = CallStep(step_id=step_id, tool_name=tool_name, arguments=arguments)
        end_session = False
        try:
            result = await asyncio.wait_for(request_tool(), timeout=timeout_seconds)
        except MCPToolCatalogChanged:
            raise
        except asyncio.TimeoutError:
            step.outcome = CallOutcome.TIMEOUT
            step.error_message = f"Call exceeded {timeout_seconds:g}s"
            end_session = True
        except McpError as exc:
            step.status_code = exc.error.code
            step.error_message = exc.error.message
            if exc.error.code == types.CONNECTION_CLOSED:
                step.outcome = CallOutcome.SESSION_ERROR
                end_session = True
            elif exc.error.code == types.INTERNAL_ERROR:
                step.outcome = CallOutcome.RPC_INTERNAL_ERROR
            else:
                step.outcome = CallOutcome.RPC_ERROR
        except Exception as exc:
            step.outcome = CallOutcome.SESSION_ERROR
            step.error_message = f"{type(exc).__name__}: {exc}"
            end_session = True
        else:
            step.structured_content = result.structuredContent
            step.raw_content = result.content or result.structuredContent
            step.result_text = "\n".join(block.text for block in result.content if block.type == "text")
            if not step.result_text and step.structured_content is not None:
                step.result_text = json.dumps(step.structured_content, ensure_ascii=False)
            if result.isError:
                step.outcome = CallOutcome.TOOL_ERROR
                step.error_message = step.result_text or "Tool returned isError"
            else:
                schema = self._output_schemas.get(tool_name)
                if schema is not None:
                    self._validate_output(step, schema)

        step.execution_time_ms = round((time.perf_counter() - start_time) * 1000, 2)
        step.is_error = step.outcome not in (CallOutcome.OK, CallOutcome.SCHEMA_UNVERIFIED)
        if end_session:
            await self.stop()
        return step

    @staticmethod
    def _validate_output(step: CallStep, schema: Dict[str, Any]) -> None:
        if step.structured_content is None:
            step.outcome = CallOutcome.SCHEMA_VIOLATION
            step.error_message = "Tool declared an output schema but returned no structured content"
            return
        try:
            # Resolve document references locally, including bundled metaschemas.
            validate(step.structured_content, schema, registry=Registry())
        except Unresolvable as exc:
            step.outcome = CallOutcome.SCHEMA_UNVERIFIED
            step.error_message = str(exc)
        except (ValidationError, SchemaError) as exc:
            step.outcome = CallOutcome.SCHEMA_VIOLATION
            step.error_message = str(exc)

    async def stop(self) -> None:
        """Close both contexts, including after a partially successful start."""
        stack, self._exit_stack = self._exit_stack, None
        self._session = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as exc:
                self.close_error = exc
