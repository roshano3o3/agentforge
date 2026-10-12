"""Tracing must stay optional for agents: agentforge_sdk.tracing (and an
agent instrumented with it) works unchanged when OpenTelemetry isn't installed."""

from __future__ import annotations

import subprocess
import sys


def test_sdk_and_agent_run_without_opentelemetry_installed() -> None:
    # A fresh interpreter where `import opentelemetry` fails: the SDK's helpers
    # are no-ops, and the instrumented agent runs exactly as before.
    code = """
import sys
sys.modules["opentelemetry"] = None
from agentforge_sdk import tracing
assert not tracing.available()
with tracing.span("x", {"a": 1}) as s:
    s.set("k", "v"); s.event("e"); s.error("nope")
assert s.span_id is None and s.context is None
tracing.annotate("k", "v")
from invoice_agent.adapter import answer_v1
out = answer_v1("Refund $40 on INV-1001 - the customer was double charged.")
assert out.answer.startswith("Refunded 40.00 USD on INV-1001"), out.answer
assert [s.span_id for s in out.steps] == [None] * 4
print("ok")
"""
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
