"""The invoice agent's MCP server and client pieces (the end-to-end parity is tests/integration/test_mcp_parity.py)."""

from __future__ import annotations

import pytest
from mcp import Client

from invoice_agent.llm_planner import tool_schemas
from invoice_agent.mcp_client import McpToolError, args_model, remote_error
from invoice_agent.mcp_server import build_server
from invoice_agent.tools import TOOLS


@pytest.mark.asyncio
async def test_the_server_lists_the_in_process_tools_with_their_published_schemas() -> None:
    async with Client(build_server()) as client:  # in-process MCP connection: real protocol messages, no subprocess
        listed = (await client.list_tools()).tools
    assert [t.name for t in listed] == [t.name for t in TOOLS]
    expected = tool_schemas(TOOLS)
    assert [{"name": t.name, "description": t.description, "parameters": t.input_schema} for t in listed] == expected
    approval = next(t for t in listed if t.name == "request_human_approval")
    assert approval.input_schema["properties"]["action"]["enum"] == ["issue_refund", "void_invoice"]


@pytest.mark.asyncio
async def test_results_and_errors_over_the_protocol() -> None:
    async with Client(build_server()) as client:
        ok = await client.call_tool("get_invoice", {"invoice_id": "INV-1002"})
        missing = await client.call_tool("get_invoice", {"invoice_id": "INV-9999"})
        too_much = await client.call_tool("issue_refund", {"invoice_id": "INV-1001", "amount": 5000})
        unknown = await client.call_tool("drop_tables", {})
    assert not ok.is_error and ok.structured_content == {
        "id": "INV-1002",
        "customer_id": "CUST-02",
        "amount": 1250.0,
        "status": "open",
        "due": "2026-10-20",
        "currency": "USD",
    }
    assert ok.content[0].text == "INV-1002: open, 1250.00 USD"
    assert missing.is_error and missing.structured_content == {
        "error_type": "LookupError",
        "message": "invoice INV-9999 not found",
    }
    assert too_much.is_error and too_much.structured_content["error_type"] == "ValueError"
    assert unknown.is_error and unknown.structured_content["message"] == "unknown tool drop_tables"


def test_a_remote_error_keeps_its_type_name_and_message() -> None:
    err = remote_error("LookupError", "invoice INV-9999 not found")
    assert type(err) is LookupError and f"{type(err).__name__}: {err}" == "LookupError: invoice INV-9999 not found"
    custom = remote_error("ToolException", "boom")
    assert isinstance(custom, McpToolError) and f"{type(custom).__name__}: {custom}" == "ToolException: boom"


def test_arguments_are_coerced_from_the_published_schema_like_in_process() -> None:
    schema = tool_schemas(TOOLS)[3]["parameters"]  # request_human_approval
    model = args_model("request_human_approval", schema)
    parsed = model.model_validate({"action": "issue_refund", "invoice_id": "INV-1001", "amount": "40.00"})
    assert parsed.amount == 40.0 and isinstance(parsed.amount, float)  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        model.model_validate({"action": "refund", "invoice_id": "INV-1001", "amount": 40})
