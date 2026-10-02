"""tracing: trace_spans (OpenTelemetry spans per run and case), agent_steps.span_id

Revision ID: c3d7e2a94b18
Revises: b9e4f1c27d36
Create Date: 2026-10-02 12:00:00

* trace_spans: every span recorded for a run, as it ended -- trace and span
  ids, parent, name, kind, recording service, timing, attributes, status,
  events. Run-level spans have no evaluation_result_id; a case's spans point
  to its result.
* agent_steps.span_id: the span the agent reported for a step (its tool
  call or decision), when it reported one. Added column only; the existing
  agent_steps triggers already freeze it with the run.
* Immutability, same rule as results, scores and steps: once a run is
  completed or failed, its spans can't be inserted, updated or deleted.

Native ALTER TABLE / CREATE TABLE only (no batch rebuild of an existing table).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d7e2a94b18"
down_revision: str | None = "b9e4f1c27d36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MSG = "trace spans are immutable once their run is completed or failed"
_FINISHED = "('completed', 'failed')"

_PG_UP = [
    f"""
    CREATE OR REPLACE FUNCTION agentforge_block_finished_run_span_mutation()
    RETURNS TRIGGER AS $$
    DECLARE
        v_status TEXT;
    BEGIN
        SELECT status::text INTO v_status FROM evaluation_runs WHERE id = COALESCE(NEW.run_id, OLD.run_id);
        IF v_status IN {_FINISHED} THEN
            RAISE EXCEPTION '{_MSG}' USING ERRCODE = '23000';
        END IF;
        RETURN COALESCE(NEW, OLD);
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    CREATE TRIGGER trg_block_finished_run_span_mutation
    BEFORE INSERT OR UPDATE OR DELETE ON trace_spans
    FOR EACH ROW EXECUTE FUNCTION agentforge_block_finished_run_span_mutation();
    """,
]
_PG_DOWN = [
    "DROP TRIGGER IF EXISTS trg_block_finished_run_span_mutation ON trace_spans;",
    "DROP FUNCTION IF EXISTS agentforge_block_finished_run_span_mutation();",
]
_SQLITE_TRIGGERS = [
    ("trg_block_finished_span_insert", "INSERT", "NEW"),
    ("trg_block_finished_span_update", "UPDATE", "OLD"),
    ("trg_block_finished_span_delete", "DELETE", "OLD"),
]


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in ("postgresql", "sqlite"):
        raise NotImplementedError(f"trace_spans migration not implemented for dialect '{dialect}'")

    op.create_table(
        "trace_spans",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("evaluation_result_id", sa.String(36), nullable=True),
        sa.Column("trace_id", sa.String(32), nullable=False),
        sa.Column("span_id", sa.String(16), nullable=False),
        sa.Column("parent_span_id", sa.String(16), nullable=True),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("service", sa.String(100), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("status_code", sa.String(10), nullable=False),
        sa.Column("status_message", sa.String(), nullable=True),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["evaluation_runs.id"]),
        sa.ForeignKeyConstraint(["evaluation_result_id"], ["evaluation_results.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id", "span_id", name="uq_trace_span"),
        sa.CheckConstraint("status_code IN ('UNSET', 'OK', 'ERROR')", name="ck_trace_span_status"),
    )
    op.create_index("ix_trace_spans_run_id", "trace_spans", ["run_id"])
    op.create_index("ix_trace_spans_evaluation_result_id", "trace_spans", ["evaluation_result_id"])
    op.create_index("ix_trace_spans_trace_id", "trace_spans", ["trace_id"])
    op.add_column("agent_steps", sa.Column("span_id", sa.String(16), nullable=True))

    if dialect == "postgresql":
        for stmt in _PG_UP:
            op.execute(stmt)
    else:
        for name, when, row in _SQLITE_TRIGGERS:
            op.execute(
                f"CREATE TRIGGER {name} BEFORE {when} ON trace_spans "
                f"WHEN (SELECT status FROM evaluation_runs WHERE id = {row}.run_id) IN {_FINISHED} "
                f"BEGIN SELECT RAISE(ABORT, '{_MSG}'); END;"
            )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        for stmt in _PG_DOWN:
            op.execute(stmt)
    else:
        for name, *_ in _SQLITE_TRIGGERS:
            op.execute(f"DROP TRIGGER IF EXISTS {name};")
    op.drop_column("agent_steps", "span_id")
    for index in ("ix_trace_spans_trace_id", "ix_trace_spans_evaluation_result_id", "ix_trace_spans_run_id"):
        op.drop_index(index, table_name="trace_spans")
    op.drop_table("trace_spans")
