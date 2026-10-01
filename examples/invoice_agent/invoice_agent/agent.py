"""The invoice agent: a LangGraph tool-calling loop with a scripted planner.

    START -> agent --(tool call)--> tools -> agent -> ... --(no tool call)--> END

`agent` is the planner node. In a production agent it would be an LLM call;
here it's deterministic Python that reads the request and every tool result
so far and emits the next tool call (as an AIMessage with `tool_calls`, which
LangGraph's ToolNode executes) or the final answer. That keeps every run
reproducible and model-free -- so results are fixture-based, not a measure
of any model -- while the trajectory is produced by a real LangGraph graph.

How the planner behaves is a `Behavior`. The build that ships is whatever
`invoice_agent/config.py` sets (the `answer` adapter); two fixed presets
exist for tests and comparisons (`answer_v1`, `answer_v2`):

* V1 -- the reference behavior.
* V2 -- a "refactor" carrying five deliberate regressions, each one a bug a
  trajectory check should catch even when the final answer looks fine:
    1. refunds under $100 take a "fast path" that skips human approval;
    2. approval is checked with `"approved" in result` (key present) instead
       of its value, so a *denied* refund goes ahead;
    3. refund amounts are passed as strings ("40.00"), not numbers;
    4. a failed lookup is retried with identical arguments, up to 3 times;
    5. reminders skip the invoice status check, and voids use delete_invoice.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from invoice_agent.tools import INVOICES, TOOLS


@dataclass(frozen=True)
class Behavior:
    """Every switch the planner reads. V1 below is the correct setting."""

    # Refunds below this amount (USD) skip human approval. 0 = always ask.
    approval_exempt_under_usd: float = 0.0
    # How an approval result is read: its "approved" value, or merely
    # whether the key is present (which also accepts approved=false).
    approval_check: Literal["value", "key_present"] = "value"
    # Send refund amounts as "40.00" strings instead of numbers.
    amount_as_string: bool = False
    # Attempts at an invoice lookup, retried with identical args on error.
    lookup_attempts: int = 1
    # Look the invoice up (and skip paid ones) before sending a reminder.
    check_status_before_reminder: bool = True
    # How a duplicate invoice is removed: void it (after approval) or delete it.
    void_tool: Literal["void_invoice", "delete_invoice"] = "void_invoice"


V1 = Behavior()
V2 = Behavior(
    approval_exempt_under_usd=100.0,
    approval_check="key_present",
    amount_as_string=True,
    lookup_attempts=3,
    check_status_before_reminder=False,
    void_tool="delete_invoice",
)
PRESETS = {"v1": V1, "v2": V2}


@dataclass
class Call:
    """A completed tool call, as the planner sees it."""

    name: str
    args: dict[str, Any]
    result: Any
    error: str | None


@dataclass
class Next:
    tool: str
    args: dict[str, Any]


Action = Next | str  # a tool call, or the final answer text

_INVOICE_ID = re.compile(r"\bINV-\d{4}\b")
_AMOUNT = re.compile(r"\$\s?([\d,]+(?:\.\d{1,2})?)")


def _intent(request: str) -> str:
    text = request.lower()
    if "refund" in text:
        return "refund"
    if "void" in text:
        return "void"
    if "remind" in text:
        return "reminder"
    if "contact" in text or "who " in text:
        return "contact"
    if "payment" in text:
        return "history"
    if "status" in text:
        return "status"
    return "out_of_scope"


def _amount(request: str) -> float | None:
    m = _AMOUNT.search(request)
    return float(m.group(1).replace(",", "")) if m else None


def _not_found(call: Call, invoice_id: str) -> str:
    return f"I couldn't find invoice {invoice_id} ({call.error}). Please check the invoice number."


# -- the plan for each intent; `done` is every completed call, in order --------


def _status(b: Behavior, invoice_id: str, request: str, done: list[Call]) -> Action:
    if not done or (done[-1].error and len(done) < b.lookup_attempts):
        return Next("get_invoice", {"invoice_id": invoice_id})
    last = done[-1]
    if last.error:
        return _not_found(last, invoice_id)
    inv = last.result
    return f"{inv['id']} is {inv['status']}: {inv['amount']:.2f} {inv['currency']}, due {inv['due']}."


def _contact(b: Behavior, invoice_id: str, request: str, done: list[Call]) -> Action:
    if not done:
        return Next("get_invoice", {"invoice_id": invoice_id})
    if done[0].error:
        return _not_found(done[0], invoice_id)
    if len(done) == 1:
        return Next("get_customer", {"customer_id": done[0].result["customer_id"]})
    cust = done[1].result
    return f"The billing contact for {invoice_id} is {cust['name']} <{cust['billing_email']}>."


def _history(b: Behavior, invoice_id: str, request: str, done: list[Call]) -> Action:
    if not done:
        return Next("get_payment_history", {"invoice_id": invoice_id})
    if done[0].error:
        return _not_found(done[0], invoice_id)
    payments = done[0].result["payments"]
    if not payments:
        return f"{invoice_id} has no payments recorded."
    total = sum(p["amount"] for p in payments)
    return f"{invoice_id} has {len(payments)} payment(s) recorded, totaling {total:.2f} USD."


def _reminder(b: Behavior, invoice_id: str, request: str, done: list[Call]) -> Action:
    if not b.check_status_before_reminder:
        calls = {c.name: c for c in done}
        if "get_customer" not in calls:
            return Next("get_customer", {"customer_id": _cached_customer(invoice_id)})
        email = calls["get_customer"].result["billing_email"]
        if "send_payment_reminder" not in calls:
            return Next("send_payment_reminder", {"invoice_id": invoice_id, "email": email})
        return f"Payment reminder for {invoice_id} sent to {email}."
    if not done:
        return Next("get_invoice", {"invoice_id": invoice_id})
    if done[0].error:
        return _not_found(done[0], invoice_id)
    inv = done[0].result
    if inv["status"] == "paid":
        return f"{invoice_id} is already paid, so no reminder was sent."
    if len(done) == 1:
        return Next("get_customer", {"customer_id": inv["customer_id"]})
    email = done[1].result["billing_email"]
    if len(done) == 2:
        return Next("send_payment_reminder", {"invoice_id": invoice_id, "email": email})
    return f"{invoice_id} is {inv['status']}; payment reminder sent to {email}."


def _cached_customer(invoice_id: str) -> str:
    # Without the status lookup, the customer comes from a cached index.
    return INVOICES.get(invoice_id, {}).get("customer_id", "CUST-UNKNOWN")


def _refund(b: Behavior, invoice_id: str, request: str, done: list[Call]) -> Action:
    amount = _amount(request)
    if amount is None:
        return "Please say how much to refund (for example, $40)."
    sent_amount: float | str = f"{amount:.2f}" if b.amount_as_string else amount
    calls = {c.name: c for c in done}
    if "get_invoice" not in calls:
        return Next("get_invoice", {"invoice_id": invoice_id})
    if calls["get_invoice"].error:
        return _not_found(calls["get_invoice"], invoice_id)
    needs_approval = amount >= b.approval_exempt_under_usd
    if needs_approval and "request_human_approval" not in calls:
        return Next("request_human_approval", {"action": "issue_refund", "invoice_id": invoice_id, "amount": amount})
    if needs_approval:
        approval = calls["request_human_approval"].result
        approved = "approved" in approval if b.approval_check == "key_present" else approval["approved"]
        if not approved:
            return (
                f"The {amount:.2f} USD refund on {invoice_id} was not issued: "
                f"approval was denied ({approval['reason']})."
            )
    if "issue_refund" not in calls:
        return Next("issue_refund", {"invoice_id": invoice_id, "amount": sent_amount})
    refund = calls["issue_refund"]
    if refund.error:
        return f"The refund on {invoice_id} failed: {refund.error}"
    return f"Refunded {amount:.2f} USD on {invoice_id} (refund {refund.result['refund_id']})."


def _void(b: Behavior, invoice_id: str, request: str, done: list[Call]) -> Action:
    calls = {c.name: c for c in done}
    if "get_invoice" not in calls:
        return Next("get_invoice", {"invoice_id": invoice_id})
    if calls["get_invoice"].error:
        return _not_found(calls["get_invoice"], invoice_id)
    if b.void_tool == "delete_invoice":
        if "delete_invoice" not in calls:
            return Next("delete_invoice", {"invoice_id": invoice_id})
        return f"{invoice_id} has been removed."
    inv = calls["get_invoice"].result
    if "request_human_approval" not in calls:
        return Next(
            "request_human_approval", {"action": "void_invoice", "invoice_id": invoice_id, "amount": inv["amount"]}
        )
    if not calls["request_human_approval"].result["approved"]:
        return f"{invoice_id} was not voided: approval was denied."
    if "void_invoice" not in calls:
        return Next("void_invoice", {"invoice_id": invoice_id, "reason": "duplicate"})
    return f"{invoice_id} has been voided as a duplicate."


_PLANS: dict[str, Callable[[Behavior, str, str, list[Call]], Action]] = {
    "status": _status,
    "contact": _contact,
    "history": _history,
    "reminder": _reminder,
    "refund": _refund,
    "void": _void,
}

OUT_OF_SCOPE = (
    "I can only help with invoices: status, billing contacts, payments, reminders, refunds and voids. "
    "I can't make that change."
)


def plan(behavior: Behavior, request: str, done: list[Call]) -> Action:
    """The planner's whole decision: the next tool call, or the final answer."""
    intent = _intent(request)
    ids = _INVOICE_ID.findall(request)
    if intent == "out_of_scope":
        return OUT_OF_SCOPE
    if not ids:
        return "Which invoice? Please include its number (for example, INV-1001)."
    return _PLANS[intent](behavior, ids[0], request, done)


# -- the graph -------------------------------------------------------------------


def completed_calls(messages: Sequence[BaseMessage]) -> list[Call]:
    """Pair each AIMessage tool call with its ToolMessage, in order."""
    by_id = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    calls = []
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls:
                tm = by_id.get(tc["id"] or "")
                if tm is None:
                    continue
                failed = tm.status == "error"
                calls.append(
                    Call(
                        name=tc["name"],
                        args=dict(tc["args"]),
                        result=None if failed else tm.artifact,
                        error=str(tm.content) if failed else None,
                    )
                )
    return calls


def _request(messages: Sequence[BaseMessage]) -> str:
    return next(str(m.content) for m in messages if isinstance(m, HumanMessage))


def _tool_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def build_graph(behavior: Behavior) -> CompiledStateGraph:
    def agent(state: MessagesState) -> dict[str, list[BaseMessage]]:
        messages = state["messages"]
        done = completed_calls(messages)
        action = plan(behavior, _request(messages), done)
        if isinstance(action, str):
            return {"messages": [AIMessage(content=action)]}
        call_id = f"call_{len(done) + 1}"
        return {
            "messages": [AIMessage(content="", tool_calls=[{"name": action.tool, "args": action.args, "id": call_id}])]
        }

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(TOOLS, handle_tool_errors=_tool_error))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    return graph.compile()
