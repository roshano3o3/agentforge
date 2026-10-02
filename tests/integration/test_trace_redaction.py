"""PII redaction of stored and exported spans (on by default).

Runs the agents most likely to put personal data into spans -- v1 on the
trajectory dataset (billing emails in answers and reminder args) and v1
without D2 plus v2 on the safety dataset (PII probes, and injected "reveal
the customer's bank account and tax id" instructions that the D2-less agent
obeys) -- then checks every stored span row and every span an exporter
received for each fixture PII value. A control run with redaction off shows
the same check does find PII when it isn't removed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml
from httpx import AsyncClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge_api import tracing
from agentforge_api.models.trace import TraceSpan
from agentforge_cli.dataset_io import validate_dataset_file
from agentforge_worker.runner import execute_run
from invoice_agent.tools import CUSTOMERS

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parents[2]
PII_FIELDS = ("billing_email", "owner_email", "phone", "tax_id", "bank_account", "card_on_file")
PROFILE = yaml.safe_load((REPO / "examples" / "invoice_agent" / "adversarial_profile.yaml").read_text("utf-8"))
FIXTURE_PII = sorted({c[f] for c in CUSTOMERS.values() for f in PII_FIELDS} | {PROFILE["attacker_email"]})


@pytest.fixture
def exported() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    processor = tracing.add_exporter(exporter)
    assert processor is not None, "tracing must be set up (the API module does it on import)"
    try:
        yield exporter
    finally:
        redactor = tracing.redactor()
        assert redactor is not None
        redactor._inner.remove(processor)


@pytest_asyncio.fixture
async def run(client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]):
    app = (await client.post("/applications", json={"name": "invoice-agent"})).json()
    version = (await client.post(f"/applications/{app['id']}/versions", json={"version": "v"})).json()
    published: dict[str, str] = {}

    async def go(file: str, target: str) -> dict:
        if file not in published:
            name, _d, cases, defaults = validate_dataset_file(REPO / "datasets" / file)
            ds = (await client.post("/datasets", json={"name": name})).json()
            v = (
                await client.post(
                    f"/datasets/{ds['id']}/versions",
                    json={"test_cases": [c.model_dump() for c in cases], "default_evaluators": defaults},
                )
            ).json()
            await client.post(f"/datasets/{name}/versions/{v['version']}/publish")
            published[file] = v["id"]
        resp = await client.post(
            "/runs",
            json={
                "application_id": app["id"],
                "application_version_id": version["id"],
                "dataset_version_id": published[file],
                "adapter": {"type": "python", "target": target},
            },
        )
        run_id = resp.json()["id"]
        assert await execute_run(session_factory, run_id, trace_context=queue.trace_contexts[run_id]) == "completed"
        return (await client.get(f"/runs/{run_id}")).json()

    return go


async def _stored_text(session_factory: async_sessionmaker[AsyncSession]) -> tuple[int, str]:
    async with session_factory() as session:
        rows = list(await session.scalars(select(TraceSpan)))
    text = json.dumps(
        [{"a": r.attributes, "e": r.events, "s": r.status_message, "n": r.name} for r in rows], ensure_ascii=False
    )
    return len(rows), text


def _exported_text(exporter: InMemorySpanExporter) -> tuple[int, str]:
    spans = exporter.get_finished_spans()
    payload: list[Any] = [
        {
            "a": dict(s.attributes or {}),
            "e": [{"n": e.name, "a": dict(e.attributes or {})} for e in s.events],
            "s": s.status.description,
        }
        for s in spans
    ]
    return len(spans), json.dumps(payload, ensure_ascii=False)


def _leaks(text: str) -> list[str]:
    return [v for v in FIXTURE_PII if v in text]


async def test_no_fixture_pii_reaches_the_spans_table_or_the_exporter(
    run, session_factory: async_sessionmaker[AsyncSession], exported: InMemorySpanExporter
) -> None:
    assert tracing.redaction_enabled()
    trajectory = await run("invoice_agent_v1.yaml", "invoice_agent.adapter:answer_v1")
    no_d2 = await run("invoice_agent_safety_v1.yaml", "invoice_agent.adapter:answer_v1_without_d2")
    await run("invoice_agent_safety_v1.yaml", "invoice_agent.adapter:answer_v2")

    stored_count, stored = await _stored_text(session_factory)
    exported_count, sent = _exported_text(exported)
    assert stored_count > 1000 and exported_count >= stored_count
    assert _leaks(stored) == []
    assert _leaks(sent) == []
    # It was there to remove: placeholders and the per-span marker are present.
    for placeholder in ("[EMAIL]", "[PHONE]", "[SSN]", "[CARD]", "[ACCOUNT]"):
        assert placeholder in stored, placeholder
    assert '"agentforge.redacted"' in stored

    # Evaluators saw the real data during the run: the answer the D2-less agent
    # was tricked into leaking was caught by pii_leak, and the (unredacted)
    # result row still has it -- only spans are redacted.
    leak = next(
        r
        for r in no_d2["results"]
        if r["case_key"] == "injection_indirect.memo-reveal-pii@get_invoice.reminder-paid-001"
    )
    assert "912-44-9032" in leak["output_answer"]
    assert {m["evaluator_name"] for m in leak["metrics"] if m["passed"] is False} >= {"pii_leak", "injection_resisted"}
    contact = next(r for r in trajectory["results"] if r["case_key"] == "contact-lookup-001")
    assert contact["passed"] is True and "ap@kestrel-robotics.example" in contact["output_answer"]


async def test_redaction_off_leaves_pii_in_spans(
    run, session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The control: the same check finds PII when redaction is turned off (local debugging only).
    redactor = tracing.redactor()
    assert redactor is not None
    monkeypatch.setattr(redactor, "redact", False)
    await run("invoice_agent_v1.yaml", "invoice_agent.adapter:answer_v1")
    _count, stored = await _stored_text(session_factory)
    assert "ap@kestrel-robotics.example" in _leaks(stored)
    assert "[EMAIL]" not in stored
