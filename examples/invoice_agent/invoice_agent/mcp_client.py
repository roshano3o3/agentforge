"""The invoice agent's tools reached over MCP, as LangChain tools the graph's ToolNode can run.

`mcp_tools(server)` connects to an MCP server -- by default it launches this package's own server
(`python -m invoice_agent.mcp_server`) as a stdio subprocess; an `http(s)://` URL connects over Streamable
HTTP instead -- and returns one tool per tool the server lists:

* **Definition from the server.** Each tool's name, description and input schema come from `tools/list`.
  The schema is kept on the tool (`metadata[MCP_SCHEMA_KEY]`), and the LLM planner offers exactly that
  schema to the model. Arguments are validated client-side with a model built from it, so the agent coerces
  arguments the way it does in-process (e.g. "40.00" -> 40.0).
* **Results.** A successful call returns `(text, structuredContent)`, LangChain's content-and-artifact
  pair, the same shape as an in-process tool.
* **Errors.** A failed call (`isError`, with `{"error_type", "message"}`) is raised as an exception of the
  same type name with the same message. The step's recorded error, "<type>: <message>", is the same as
  in-process.

The scenario's tool overrides and the `execute_tool` spans wrap these tools exactly as they wrap the
in-process ones (tools.py `tools_for`), so trajectories, stored spans and verdicts don't depend on the
transport. The MCP SDK records its own client spans; they run in the agent's trace context (so an OTLP
exporter shows them under `execute_tool`, and the traceparent reaches the server in `_meta`), but AgentForge
stores only its own instrumentation scopes, so they never reach `trace_spans`.

One connection per server per process, opened on first use and kept for the life of the process (the
worker's): the server is stateless, so cases can share it.
"""

from __future__ import annotations

import asyncio
import atexit
import contextvars
import sys
import threading
from typing import Any, Literal

import mcp_types as types
from langchain_core.tools import BaseTool, StructuredTool
from mcp import Client, StdioServerParameters
from pydantic import BaseModel, Field, create_model

MCP_SCHEMA_KEY = "agentforge_mcp_input_schema"
CONNECT_TIMEOUT_S = 60.0
CALL_TIMEOUT_S = 60.0

_BUILTIN_ERRORS: dict[str, type[Exception]] = {
    e.__name__: e
    for e in (
        TimeoutError,
        ConnectionError,
        PermissionError,
        RuntimeError,
        LookupError,
        ValueError,
        KeyError,
        TypeError,
    )
}
_JSON_TYPES: dict[str, Any] = {"string": str, "number": float, "integer": int, "boolean": bool}


class McpToolError(Exception):
    """Base for a server-side tool error whose type isn't a Python builtin (its type name is kept)."""


def remote_error(error_type: str, message: str) -> Exception:
    """The exception a failed MCP call is raised as: the builtin of that name, else a subclass named after it."""
    cls = _BUILTIN_ERRORS.get(error_type) or type(error_type, (McpToolError,), {})
    return cls(message)


class Connection:
    """A live MCP client session, driven by its own event loop thread (the graph's tools are synchronous)."""

    def __init__(self, server: str) -> None:
        self.server = server
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._client: Client | None = None
        self._error: BaseException | None = None
        self.tools: list[types.Tool] = []
        self._thread = threading.Thread(target=self._loop.run_forever, name=f"mcp:{server or 'stdio'}", daemon=True)
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._main(), self._loop)
        if not self._ready.wait(CONNECT_TIMEOUT_S):
            raise ConnectionError(f"MCP server {self.label} didn't answer within {CONNECT_TIMEOUT_S:g}s")
        if self._error is not None:
            raise ConnectionError(f"MCP server {self.label}: {self._error}") from None

    @property
    def label(self) -> str:
        return self.server or "stdio: python -m invoice_agent.mcp_server"

    def _target(self) -> str | StdioServerParameters:
        if self.server:
            return self.server
        return StdioServerParameters(command=sys.executable, args=["-m", "invoice_agent.mcp_server"])

    async def _main(self) -> None:
        self._stop = asyncio.Event()
        try:
            async with Client(self._target()) as client:
                self._client = client
                self.tools = list((await client.list_tools()).tools)
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - reported to the waiting caller
            self._error = exc
        finally:
            self._client = None
            self._ready.set()

    @property
    def alive(self) -> bool:
        return self._client is not None and self._thread.is_alive()

    def call(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        client = self._client
        if client is None:
            raise ConnectionError(f"MCP server {self.label} is not connected")
        ctx = contextvars.copy_context()  # the caller's trace context, for the SDK's own spans

        async def in_caller_context() -> types.CallToolResult:
            return await asyncio.get_running_loop().create_task(client.call_tool(name, arguments), context=ctx)

        return asyncio.run_coroutine_threadsafe(in_caller_context(), self._loop).result(CALL_TIMEOUT_S)

    def close(self) -> None:
        if self._stop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop.set)
            self._thread.join(timeout=5)
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)


_connections: dict[str, Connection] = {}
_lock = threading.Lock()


def connection(server: str = "") -> Connection:
    """The process's connection to `server` ("" = launch the bundled server over stdio), (re)opened as needed."""
    with _lock:
        conn = _connections.get(server)
        if conn is None or not conn.alive:
            conn = _connections[server] = Connection(server)
        return conn


@atexit.register
def _close_all() -> None:
    for conn in list(_connections.values()):
        conn.close()


def args_model(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """A pydantic model for a tool's JSON input schema (flat properties: string/number/integer/boolean/enum)."""
    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}
    for prop, spec in (schema.get("properties") or {}).items():
        if "enum" in spec:
            annotation: Any = Literal[tuple(spec["enum"])]
        else:
            annotation = _JSON_TYPES.get(spec.get("type", ""), Any)
        default = ... if prop in required else None
        fields[prop] = (
            annotation if prop in required else annotation | None,
            Field(default, description=spec.get("description")),
        )
    return create_model(name, **fields)


def _client_tool(conn: Connection, listed: types.Tool) -> BaseTool:
    def fn(**kwargs: Any) -> tuple[str, dict[str, Any]]:
        result = conn.call(listed.name, kwargs)
        data = result.structured_content or {}
        if result.is_error:
            raise remote_error(str(data.get("error_type", "RuntimeError")), str(data.get("message", "")))
        text = "".join(c.text for c in result.content if isinstance(c, types.TextContent))
        return text, data

    return StructuredTool.from_function(
        func=fn,
        name=listed.name,
        description=listed.description or "",
        args_schema=args_model(listed.name, dict(listed.input_schema)),
        response_format="content_and_artifact",
        metadata={MCP_SCHEMA_KEY: dict(listed.input_schema)},
    )


def mcp_tools(server: str = "") -> list[BaseTool]:
    """One LangChain tool per tool the MCP server lists, in the server's order."""
    conn = connection(server)
    return [_client_tool(conn, t) for t in conn.tools]
