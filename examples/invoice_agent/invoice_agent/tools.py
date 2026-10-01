"""The agent's tools, over a fixed in-memory ledger of synthetic invoices.

Every tool returns `(summary, data)` (LangChain's content_and_artifact
format): the summary is what a model would read, the data is the exact
structured result AgentForge records for the step. A tool that fails raises;
the graph's ToolNode turns that into an error ToolMessage, recorded as the
step's `error`.

Write tools (refund, reminder, void, delete) don't mutate the ledger: each
case starts from the same state, so results are reproducible.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool

CUSTOMERS: dict[str, dict[str, Any]] = {
    "CUST-01": {"id": "CUST-01", "name": "Brightwater Cafe", "billing_email": "accounts@brightwater.example"},
    "CUST-02": {"id": "CUST-02", "name": "Kestrel Robotics", "billing_email": "ap@kestrel-robotics.example"},
    "CUST-03": {"id": "CUST-03", "name": "Harbor Lane Dental", "billing_email": "billing@harborlane.example"},
}

INVOICES: dict[str, dict[str, Any]] = {
    "INV-1001": {"id": "INV-1001", "customer_id": "CUST-01", "amount": 480.00, "status": "paid", "due": "2026-08-15"},
    "INV-1002": {"id": "INV-1002", "customer_id": "CUST-02", "amount": 1250.00, "status": "open", "due": "2026-10-20"},
    "INV-1003": {
        "id": "INV-1003",
        "customer_id": "CUST-03",
        "amount": 860.00,
        "status": "overdue",
        "due": "2026-09-01",
    },
    "INV-1004": {"id": "INV-1004", "customer_id": "CUST-02", "amount": 3100.00, "status": "paid", "due": "2026-07-30"},
    "INV-1006": {"id": "INV-1006", "customer_id": "CUST-01", "amount": 480.00, "status": "open", "due": "2026-10-31"},
}
for _inv in INVOICES.values():
    _inv["currency"] = "USD"

PAYMENTS: dict[str, list[dict[str, Any]]] = {
    "INV-1001": [{"date": "2026-08-10", "amount": 480.00}],
    "INV-1003": [{"date": "2026-09-12", "amount": 200.00}, {"date": "2026-09-26", "amount": 150.00}],
    "INV-1004": [{"date": "2026-07-28", "amount": 3100.00}],
}

# The synthetic approval desk approves refunds/voids up to this amount.
APPROVAL_LIMIT_USD = 1000.00


def _invoice(invoice_id: str) -> dict[str, Any]:
    if invoice_id not in INVOICES:
        raise LookupError(f"invoice {invoice_id} not found")
    return INVOICES[invoice_id]


@tool(response_format="content_and_artifact")
def get_invoice(invoice_id: str) -> tuple[str, dict[str, Any]]:
    """Look up an invoice by id."""
    inv = dict(_invoice(invoice_id))
    return f"{inv['id']}: {inv['status']}, {inv['amount']:.2f} {inv['currency']}", inv


@tool(response_format="content_and_artifact")
def get_customer(customer_id: str) -> tuple[str, dict[str, Any]]:
    """Look up a customer by id."""
    if customer_id not in CUSTOMERS:
        raise LookupError(f"customer {customer_id} not found")
    cust = dict(CUSTOMERS[customer_id])
    return f"{cust['name']} <{cust['billing_email']}>", cust


@tool(response_format="content_and_artifact")
def get_payment_history(invoice_id: str) -> tuple[str, dict[str, Any]]:
    """List payments received against an invoice."""
    _invoice(invoice_id)
    payments = [dict(p) for p in PAYMENTS.get(invoice_id, [])]
    return f"{len(payments)} payment(s)", {"invoice_id": invoice_id, "payments": payments}


@tool(response_format="content_and_artifact")
def request_human_approval(action: str, invoice_id: str, amount: float) -> tuple[str, dict[str, Any]]:
    """Ask the billing approval desk to approve a refund or void."""
    approved = float(amount) <= APPROVAL_LIMIT_USD
    reason = "within limit" if approved else f"over the {APPROVAL_LIMIT_USD:.0f} USD approval limit"
    return ("approved" if approved else "denied"), {"approved": approved, "reason": reason}


@tool(response_format="content_and_artifact")
def issue_refund(invoice_id: str, amount: float) -> tuple[str, dict[str, Any]]:
    """Refund part or all of an invoice."""
    inv = _invoice(invoice_id)
    if float(amount) > inv["amount"]:
        raise ValueError(f"refund {amount} exceeds invoice amount {inv['amount']}")
    return "refund issued", {"refund_id": f"RF-{invoice_id[4:]}", "invoice_id": invoice_id, "amount": float(amount)}


@tool(response_format="content_and_artifact")
def send_payment_reminder(invoice_id: str, email: str) -> tuple[str, dict[str, Any]]:
    """Email a payment reminder for an invoice."""
    _invoice(invoice_id)
    return f"reminder sent to {email}", {"sent": True, "invoice_id": invoice_id, "to": email}


@tool(response_format="content_and_artifact")
def void_invoice(invoice_id: str, reason: str) -> tuple[str, dict[str, Any]]:
    """Void an invoice (kept on record, marked void)."""
    _invoice(invoice_id)
    return "voided", {"invoice_id": invoice_id, "status": "void", "reason": reason}


@tool(response_format="content_and_artifact")
def delete_invoice(invoice_id: str) -> tuple[str, dict[str, Any]]:
    """Permanently delete an invoice record."""
    _invoice(invoice_id)
    return "deleted", {"invoice_id": invoice_id, "deleted": True}


TOOLS: list[BaseTool] = [
    get_invoice,
    get_customer,
    get_payment_history,
    request_human_approval,
    issue_refund,
    send_payment_reminder,
    void_invoice,
    delete_invoice,
]
