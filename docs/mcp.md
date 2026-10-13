# MCP: evaluating agents whose tools live behind MCP servers

## Why

More and more agents don't call their tools as functions in the same process. They reach them through
[Model Context Protocol](https://modelcontextprotocol.io/) servers: a ticketing system's MCP server, a database's,
an internal API wrapped as one. To evaluate such an agent, AgentForge has to see the same trajectory, spans and
verdicts as for an in-process agent. Otherwise moving a tool behind MCP would look like a behaviour change, and a
regression in the MCP layer couldn't be told apart from a change in the agent.

So the example invoice agent can reach its tools either way:

- **In-process (the default).** The LangChain tools in [`tools.py`](../examples/invoice_agent/invoice_agent/tools.py).
- **Over MCP.** The same tools served by [`invoice_agent.mcp_server`](../examples/invoice_agent/invoice_agent/mcp_server.py) and called through [`invoice_agent.mcp_client`](../examples/invoice_agent/invoice_agent/mcp_client.py), using the official MCP Python SDK on both sides.

A test then proves the evaluation can't tell the two apart (below).

## How it works

```
planner (scripted or LLM)
  -> ToolNode
     -> execute_tool span            (tools.py _traced: the same wrapper either way)
        -> scenario override         (tools.py _overridden: test-harness fault injection, before the transport)
           -> in-process: the tool function
           -> MCP: mcp_client tool -> tools/call over stdio or Streamable HTTP -> mcp_server -> the tool function
```

**The server** (`python -m invoice_agent.mcp_server`) serves stdio by default; `--http PORT` serves Streamable HTTP on `127.0.0.1:PORT/mcp`.
- **`tools/list`** publishes each tool's name, description and input schema. These are exactly the schemas the in-process agent offers a model, generated from the tools' own signatures.
- **`tools/call`** validates the arguments with the tool's own argument model and runs the tool. It returns the summary as text content and the structured result as `structuredContent`. A failure returns `isError` with `{"error_type", "message"}`.

**The client** builds one LangChain tool per listed tool:
- **Arguments:** validated client-side against the published schema, so the same coercion happens as in-process (e.g. v2's string amount `"40.00"` becomes `40.0`).
- **Results:** returned as LangChain's `(content, artifact)` pair.
- **Errors:** raised with the same type name and message, so the step's error is the same `"LookupError: invoice INV-9999 not found"`.
- **Schemas offered to a model:** the LLM planner offers the schema the server published.
- **Connections:** one per process, opened on first use and reused (the server is stateless).

**The scenario stays in the agent's tool layer.** A test case's injected tool failure, extra fields merged into a result, or session user is set up by the test harness. It is never sent to the MCP server. Fault injection therefore happens before the transport, so a scripted `TimeoutError` never reaches the server.

**Spans.**
- **Stored spans:** the agent's `execute_tool` spans wrap the MCP call exactly as they wrap the function.
- **The SDK's own spans:** the MCP SDK records client spans in the agent's trace context, and the traceparent reaches the server in `_meta`. An OTLP exporter therefore shows them under `execute_tool`. AgentForge stores only its own instrumentation scopes, so they never reach `trace_spans`.
- **Server spans:** the server records none.

## Use it

Both the scripted adapters (`answer`, `answer_v1`, `answer_v2`, `answer_v1_without_d2`) and the LLM planner (`answer_llm`) declare two settings:

| Setting | Values | Default |
|---|---|---|
| `tools` | `in-process`, `mcp` | `in-process` |
| `mcp_server` | an `http(s)://…/mcp` URL (Streamable HTTP); empty launches the bundled server over stdio | empty |

- **As a run's adapter settings:** the API's `adapter_settings`, which `agentforge compare` sets for LLM runs.
- **As a replay override:** `agentforge replay <result> --set tools=mcp`, or the Replay form on the Trace Explorer.

The worker image includes the server, so `tools=mcp` with no URL works in Docker Compose and Kubernetes as is.

## Parity, tested

[`tests/integration/test_mcp_parity.py`](../tests/integration/test_mcp_parity.py) checks four agents: the scripted planner as v1, v2 and v1 without D2 (demo PR #4), and the LLM planner driven by a fake provider. Each one runs every case of the trajectory dataset (10) and the safety dataset (35) through the real API and worker, twice: tools in-process, and tools over MCP (a real stdio subprocess). That's 180 case pairs. For every pair:

- **Result:** status, verdict, answer, tokens and model are identical.
- **Trajectory:** every step's tool, args, result or error, and output is identical.
- **Evaluators:** every evaluator's version, score, value, verdict, reason and evidence is identical.
- **Stored span tree:** names, nesting, attributes, status and events are identical. For the LLM planner, the tool schemas offered to the model on every turn are identical too.

What the comparison ignores:

- **Measured values:** timings, ids, and the latency evaluator's measured value.
- **One genuine difference:** a tool-error event's `exception.stacktrace`. Over MCP it shows the client re-raising the server's error rather than the tool's own frames; its `exception.type` and `exception.message` are identical. The stack trace describes where code ran, not what the agent did.

[`tests/unit/test_mcp_tools.py`](../tests/unit/test_mcp_tools.py) also covers the server over an in-process MCP connection (listing, results, error shapes, an unknown tool) and the client's argument coercion and error mapping. An unreachable server fails each case with an error naming it, never a silent fallback to in-process.

## Limits

- **Latency is the transport's.** The first case on a fresh worker includes launching the stdio server (about 3 s in the test, on Windows). A remote server adds network time to every tool call. Latency isn't comparable across transports and the parity test ignores it.
- **The example's MCP server serves the example's tools.** For your own agent, point `mcp_server` at its MCP server URL and write an adapter that builds its tools with `mcp_client.mcp_tools(url)`. The client assumes flat argument schemas (string, number, integer, boolean, enum), which is what the example needs; nested object arguments aren't mapped yet.
- **No server-side spans are collected.** The traceparent reaches the server, but an MCP server exporting its own spans is up to it (as with HTTP agents).
- **The client trusts the server's tool list.** A server that lists different tools changes what the agent can call, and that would show up in the trajectory, not as a transport error.
