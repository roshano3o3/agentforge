"""agent trajectory: recorded steps (tool calls) + per-case trajectory expectations

Revision ID: e8b4c2d61a9f
Revises: d7a3e5c19f2b
Create Date: 2026-09-30 20:00:00

* agent_steps: one row per step an agent reported for one case's result, in
  order (`step_index` is 1-based and counts every step -- retrieval, tool
  calls, the final answer -- the same "step N" evaluator reasons and the
  dashboard use). A `tool_call` step keeps the tool name, its args, and
  exactly what came back: `result` or `error`.
* test_cases.trajectory: the case's trajectory expectations (expected_tools,
  forbidden_tools, expected_sequence, ...; see agentforge_evaluators.trajectory).
  Added column only: existing rows aren't rewritten, and the published-version
  test_cases trigger from b17cf04aecaf already freezes every column of a
  published case, this one included.
* Immutability, same rule as results and metric scores: once a run is
  completed or failed, its steps can't be inserted, updated or deleted.

Native ALTER TABLE / CREATE TABLE only, as in c4e2a91d7b3f (a SQLite batch
rebuild of test_cases would drop the dataset immutability triggers).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8b4c2d61a9f"
down_revision: str | None = "d7a3e5c19f2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STEPS_MSG = "agent steps are immutable once their run is completed or failed"
_FINISHED = "('completed', 'failed')"
_STEP_RUN_STATUS = (
    "(SELECT r.status FROM evaluation_results er JOIN evaluation_runs r ON r.id = er.evaluation_run_id "
    "WHERE er.id = {row}.evaluation_result_id)"
)

_PG_UP = [
    f"""
    CREATE OR REPLACE FUNCTION agentforge_block_finished_run_step_mutation()
    RETURNS TRIGGER AS $$
    DECLARE
        v_status TEXT;
    BEGIN
        SELECT r.status::text INTO v_status
        FROM evaluation_results er JOIN evaluation_runs r ON r.id = er.evaluation_run_id
        WHERE er.id = COALESCE(NEW.evaluation_result_id, OLD.evaluation_result_id);
        IF v_status IN {_FINISHED} THEN
            RAISE EXCEPTION '{_STEPS_MSG}'
                USING ERRCODE = '23000';
        END IF;
        RETURN COALESCE(NEW, OLD);
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    CREATE TRIGGER trg_block_finished_run_step_mutation
    BEFORE INSERT OR UPDATE OR DELETE ON agent_steps
    FOR EACH ROW EXECUTE FUNCTION agentforge_block_finished_run_step_mutation();
    """,
]

_PG_DOWN = [
    "DROP TRIGGER IF EXISTS trg_block_finished_run_step_mutation ON agent_steps;",
    "DROP FUNCTION IF EXISTS agentforge_block_finished_run_step_mutation();",
]

_SQLITE_TRIGGERS = [
    ("trg_block_finished_step_insert", "INSERT", "NEW"),
    ("trg_block_finished_step_update", "UPDATE", "OLD"),
    ("trg_block_finished_step_delete", "DELETE", "OLD"),
]


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in ("postgresql", "sqlite"):
        raise NotImplementedError(f"agent trajectory migration not implemented for dialect '{dialect}'")

    op.add_column("test_cases", sa.Column("trajectory", sa.JSON(), nullable=True))

    op.create_table(
        "agent_steps",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("evaluation_result_id", sa.String(36), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("args", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("retrieved_doc_ids", sa.JSON(), nullable=False),
        sa.Column("output", sa.String(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["evaluation_result_id"], ["evaluation_results.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evaluation_result_id", "step_index", name="uq_agent_step_index"),
        sa.CheckConstraint("kind IN ('retrieval', 'tool_call', 'final_answer')", name="ck_agent_step_kind"),
        sa.CheckConstraint("step_index >= 1", name="ck_agent_step_index_positive"),
    )
    op.create_index("ix_agent_steps_evaluation_result_id", "agent_steps", ["evaluation_result_id"])

    if dialect == "postgresql":
        for stmt in _PG_UP:
            op.execute(stmt)
    else:
        for name, when, row in _SQLITE_TRIGGERS:
            op.execute(
                f"CREATE TRIGGER {name} BEFORE {when} ON agent_steps "
                f"WHEN {_STEP_RUN_STATUS.format(row=row)} IN {_FINISHED} "
                f"BEGIN SELECT RAISE(ABORT, '{_STEPS_MSG}'); END;"
            )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        for stmt in _PG_DOWN:
            op.execute(stmt)
    else:
        for name, *_ in _SQLITE_TRIGGERS:
            op.execute(f"DROP TRIGGER IF EXISTS {name};")
    # Dropping the table discards recorded steps; they can't be represented
    # by the previous schema.
    op.drop_index("ix_agent_steps_evaluation_result_id", table_name="agent_steps")
    op.drop_table("agent_steps")
    op.drop_column("test_cases", "trajectory")
