"""failure replay: replays + their steps, metric scores and spans

Revision ID: d4a8f2c61e57
Revises: c3d7e2a94b18
Create Date: 2026-10-06 12:00:00

* replays: one evaluated case re-run (possibly with overrides), linked to the
  original result; copies the original run's adapter, pinned evaluators,
  threshold, latency budget and timeout; holds the replayed case's outcome.
* replay_steps / replay_metric_scores / replay_spans: the replay's own
  trajectory, evaluator verdicts and spans, shaped like agent_steps,
  metric_scores and trace_spans. The original run's tables are not changed.
* Immutability, same rule as runs: once a replay is completed or failed it
  can't be updated or deleted, and its steps, scores and spans can't be
  inserted, updated or deleted.

New tables only (no change to an existing table).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4a8f2c61e57"
down_revision: str | None = "c3d7e2a94b18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FINISHED = "('completed', 'failed')"
_REPLAY_MSG = "replays are immutable once completed or failed"
_CHILD_MSG = "replay steps, scores and spans are immutable once the replay is completed or failed"
_CHILDREN = ("replay_steps", "replay_metric_scores", "replay_spans")

_PG_UP = [
    f"""
    CREATE OR REPLACE FUNCTION agentforge_block_finished_replay_mutation()
    RETURNS TRIGGER AS $$
    BEGIN
        IF OLD.status IN {_FINISHED} THEN
            RAISE EXCEPTION '{_REPLAY_MSG}' USING ERRCODE = '23000';
        END IF;
        RETURN COALESCE(NEW, OLD);
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    CREATE TRIGGER trg_block_finished_replay_mutation
    BEFORE UPDATE OR DELETE ON replays
    FOR EACH ROW EXECUTE FUNCTION agentforge_block_finished_replay_mutation();
    """,
    f"""
    CREATE OR REPLACE FUNCTION agentforge_block_finished_replay_child_mutation()
    RETURNS TRIGGER AS $$
    DECLARE
        v_status TEXT;
    BEGIN
        SELECT status INTO v_status FROM replays WHERE id = COALESCE(NEW.replay_id, OLD.replay_id);
        IF v_status IN {_FINISHED} THEN
            RAISE EXCEPTION '{_CHILD_MSG}' USING ERRCODE = '23000';
        END IF;
        RETURN COALESCE(NEW, OLD);
    END;
    $$ LANGUAGE plpgsql;
    """,
    *(
        f"""
        CREATE TRIGGER trg_block_finished_{table}_mutation
        BEFORE INSERT OR UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION agentforge_block_finished_replay_child_mutation();
        """
        for table in _CHILDREN
    ),
]
_PG_DOWN = [
    *(f"DROP TRIGGER IF EXISTS trg_block_finished_{table}_mutation ON {table};" for table in _CHILDREN),
    "DROP FUNCTION IF EXISTS agentforge_block_finished_replay_child_mutation();",
    "DROP TRIGGER IF EXISTS trg_block_finished_replay_mutation ON replays;",
    "DROP FUNCTION IF EXISTS agentforge_block_finished_replay_mutation();",
]


def _sqlite_triggers() -> list[tuple[str, str]]:
    """(name, CREATE TRIGGER statement) for every SQLite trigger."""
    out = [
        (
            f"trg_block_finished_replay_{op_name.lower()}",
            f"CREATE TRIGGER trg_block_finished_replay_{op_name.lower()} BEFORE {op_name} ON replays "
            f"WHEN OLD.status IN {_FINISHED} BEGIN SELECT RAISE(ABORT, '{_REPLAY_MSG}'); END;",
        )
        for op_name in ("UPDATE", "DELETE")
    ]
    for table in _CHILDREN:
        for op_name, row in (("INSERT", "NEW"), ("UPDATE", "OLD"), ("DELETE", "OLD")):
            name = f"trg_block_finished_{table}_{op_name.lower()}"
            out.append(
                (
                    name,
                    f"CREATE TRIGGER {name} BEFORE {op_name} ON {table} "
                    f"WHEN (SELECT status FROM replays WHERE id = {row}.replay_id) IN {_FINISHED} "
                    f"BEGIN SELECT RAISE(ABORT, '{_CHILD_MSG}'); END;",
                )
            )
    return out


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in ("postgresql", "sqlite"):
        raise NotImplementedError(f"failure replay migration not implemented for dialect '{dialect}'")

    op.create_table(
        "replays",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("original_result_id", sa.String(36), nullable=False),
        sa.Column("original_run_id", sa.String(36), nullable=False),
        sa.Column("test_case_id", sa.String(36), nullable=False),
        sa.Column("dataset_version_id", sa.String(36), nullable=False),
        sa.Column("dataset_content_hash", sa.String(80), nullable=True),
        sa.Column("adapter_type", sa.String(20), nullable=False),
        sa.Column("adapter_target", sa.String(2000), nullable=False),
        sa.Column("evaluators", sa.JSON(), nullable=False),
        sa.Column("provider_type", sa.String(100), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("max_latency_ms", sa.Float(), nullable=True),
        sa.Column("timeout_seconds", sa.Float(), nullable=True),
        sa.Column("overrides", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("result_status", sa.String(10), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("output_answer", sa.String(), nullable=True),
        sa.Column("retrieved_doc_ids", sa.JSON(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("error_type", sa.String(200), nullable=True),
        sa.Column("case_error_message", sa.String(), nullable=True),
        sa.Column("trace_id", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["original_result_id"], ["evaluation_results.id"]),
        sa.ForeignKeyConstraint(["original_run_id"], ["evaluation_runs.id"]),
        sa.ForeignKeyConstraint(["test_case_id"], ["test_cases.id"]),
        sa.ForeignKeyConstraint(["dataset_version_id"], ["dataset_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status IN ('pending', 'running', 'completed', 'failed')", name="ck_replay_status"),
        sa.CheckConstraint(
            "result_status IS NULL OR result_status IN ('ok', 'error', 'timeout')", name="ck_replay_result_status"
        ),
    )
    op.create_index("ix_replays_original_result_id", "replays", ["original_result_id"])

    op.create_table(
        "replay_metric_scores",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("replay_id", sa.String(36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("evaluator_name", sa.String(100), nullable=False),
        sa.Column("evaluator_version", sa.String(50), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(20), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["replay_id"], ["replays.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_replay_metric_scores_replay_id", "replay_metric_scores", ["replay_id"])

    op.create_table(
        "replay_steps",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("replay_id", sa.String(36), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("args", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("retrieved_doc_ids", sa.JSON(), nullable=False),
        sa.Column("output", sa.String(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("span_id", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["replay_id"], ["replays.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("replay_id", "step_index", name="uq_replay_step_index"),
        sa.CheckConstraint("kind IN ('retrieval', 'tool_call', 'final_answer')", name="ck_replay_step_kind"),
        sa.CheckConstraint("step_index >= 1", name="ck_replay_step_index_positive"),
    )
    op.create_index("ix_replay_steps_replay_id", "replay_steps", ["replay_id"])

    op.create_table(
        "replay_spans",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("replay_id", sa.String(36), nullable=False),
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
        sa.ForeignKeyConstraint(["replay_id"], ["replays.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id", "span_id", name="uq_replay_span"),
        sa.CheckConstraint("status_code IN ('UNSET', 'OK', 'ERROR')", name="ck_replay_span_status"),
    )
    op.create_index("ix_replay_spans_replay_id", "replay_spans", ["replay_id"])

    if dialect == "postgresql":
        for stmt in _PG_UP:
            op.execute(stmt)
    else:
        for _name, stmt in _sqlite_triggers():
            op.execute(stmt)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        for stmt in _PG_DOWN:
            op.execute(stmt)
    else:
        for name, _stmt in _sqlite_triggers():
            op.execute(f"DROP TRIGGER IF EXISTS {name};")
    for table in ("replay_spans", "replay_steps", "replay_metric_scores"):
        op.drop_index(f"ix_{table}_replay_id", table_name=table)
        op.drop_table(table)
    op.drop_index("ix_replays_original_result_id", table_name="replays")
    op.drop_table("replays")
