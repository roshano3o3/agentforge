"""The invoice agent's mock tools as an MCP server.

    python -m invoice_agent.mcp_server            # stdio (what the agent launches by default)
    python -m invoice_agent.mcp_server --http 8765 # Streamable HTTP on 127.0.0.1:8765/mcp

The same eight tools as tools.py, over the same synthetic ledger, served with the official MCP Python SDK's
low-level `Server`:

* **tools/list** publishes each tool's name, description and input schema exactly as the in-process agent
  offers them to a model (LangChain's JSON schema of the tool's own signature), so a model sees the same
  tool definitions whichever way the tools are reached.
* **tools/call** validates the arguments with the tool's own argument model, runs it, and returns the
  model-readable summary as text content and the structured result as `structuredContent`.
* A tool that fails returns `isError: true` with `structuredContent = {"error_type", "message"}` (and the
  text "<type>: <message>"), so the client can report the same error the in-process tool raised.

The server is stateless, like the in-process tools (write tools don't mutate the ledger). A test case's
scenario (an injected tool failure, extra fields merged into a result, the session user) is not sent here:
it's test-harness setup and stays in the agent's tool layer, applied to whatever transport the tools use.
The server records no spans; the agent's `execute_tool` spans wrap the MCP call (mcp_client.py).
"""

from __future__ import annotations

import argparse
from typing import Any

import anyio
import mcp_types as types
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from pydantic import BaseModel

from invoice_agent.tools import TOOLS

SERVER_NAME = "agentforge-invoice-tools"


def published_schema(tool: BaseTool) -> tuple[str, dict[str, Any]]:
    """(description, input JSON schema) of a tool, as the in-process agent offers it to a model."""
    fn = convert_to_openai_tool(tool)["function"]
    return fn.get("description", ""), fn["parameters"]


def run_tool(tool: BaseTool, arguments: dict[str, Any]) -> types.CallToolResult:
    try:
        schema = tool.args_schema
        assert isinstance(schema, type) and issubclass(schema, BaseModel)  # every tool here has a pydantic model
        validated = schema.model_validate(arguments)
        kwargs = {name: getattr(validated, name) for name in type(validated).model_fields}
        content, data = tool.func(**kwargs)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - every tool failure goes back to the client as data
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"{type(exc).__name__}: {exc}")],
            structured_content={"error_type": type(exc).__name__, "message": str(exc)},
            is_error=True,
        )
    return types.CallToolResult(content=[types.TextContent(type="text", text=content)], structured_content=data)


def build_server() -> Server[Any]:
    tools = {t.name: t for t in TOOLS}

    async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        listed = []
        for t in TOOLS:
            description, schema = published_schema(t)
            listed.append(types.Tool(name=t.name, description=description, input_schema=schema))
        return types.ListToolsResult(tools=listed)

    async def call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        tool = tools.get(params.name)
        if tool is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"LookupError: unknown tool {params.name}")],
                structured_content={"error_type": "LookupError", "message": f"unknown tool {params.name}"},
                is_error=True,
            )
        return run_tool(tool, dict(params.arguments or {}))

    return Server(SERVER_NAME, version="1.0.0", on_list_tools=list_tools, on_call_tool=call_tool)


async def _serve_stdio() -> None:
    server = build_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--http", type=int, metavar="PORT", help="Serve Streamable HTTP on 127.0.0.1:PORT/mcp")
    args = parser.parse_args()
    if args.http is None:
        anyio.run(_serve_stdio)
        return
    import uvicorn

    uvicorn.run(build_server().streamable_http_app(), host="127.0.0.1", port=args.http, log_level="warning")


if __name__ == "__main__":
    main()
