"""re-scores: a finished run's stored outputs scored again with other evaluator versions

Revision ID: a9c3e7d15b42
Revises: f8b2d6c43a19
Create Date: 2026-10-08 12:00:00

* rescores: one re-score of a finished run (requested evaluators, the full pinned list it scored
  with, which versions changed, status, aggregates, provenance).
* rescore_results / rescore_metric_scores: one row per original result (its new case verdict) and
  one per evaluator verdict, shaped like metric_scores. The original run's tables are not changed.
* Immutability, same rule as runs and replays: once a re-score is completed or failed it can't be
  updated or deleted, and its results and scores can't be inserted, updated or deleted.

New tables only (no change to an existing table).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9c3e7d15b42"
down_revision: str | None = "f8b2d6c43a19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FINISHED = "('completed', 'failed')"
_RESCORE_MSG = "re-scores are immutable once completed or failed"
_CHILD_MSG = "re-score results and scores are immutable once the re-score is completed or failed"
_CHILDREN = ("rescore_results", "rescore_metric_scores")

_PG_UP = [
    f"""
    CREATE OR REPLACE FUNCTION agentforge_block_finished_rescore_mutation()
    RETURNS TRIGGER AS $$
    BEGIN
        IF OLD.status IN {_FINISHED} THEN
            RAISE EXCEPTION '{_RESCORE_MSG}' USING ERRCODE = '23000';
        END IF;
        RETURN COALESCE(NEW, OLD);
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    CREATE TRIGGER trg_block_finished_rescore_mutation
    BEFORE UPDATE OR DELETE ON rescores
    FOR EACH ROW EXECUTE FUNCTION agentforge_block_finished_rescore_mutation();
    """,
    f"""
    CREATE OR REPLACE FUNCTION agentforge_block_finished_rescore_child_mutation()
    RETURNS TRIGGER AS $$
    DECLARE
        v_status TEXT;
    BEGIN
        SELECT status INTO v_status FROM rescores WHERE id = COALESCE(NEW.rescore_id, OLD.rescore_id);
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
        FOR EACH ROW EXECUTE FUNCTION agentforge_block_finished_rescore_child_mutation();
        """
        for table in _CHILDREN
    ),
]
_PG_DOWN = [
    *(f"DROP TRIGGER IF EXISTS trg_block_finished_{table}_mutation ON {table};" for table in _CHILDREN),
    "DROP FUNCTION IF EXISTS agentforge_block_finished_rescore_child_mutation();",
    "DROP TRIGGER IF EXISTS trg_block_finished_rescore_mutation ON rescores;",
    "DROP FUNCTION IF EXISTS agentforge_block_finished_rescore_mutation();",
]


def _sqlite_triggers() -> list[tuple[str, str]]:
    """(name, CREATE TRIGGER statement) for every SQLite trigger."""
    out = [
        (
            f"trg_block_finished_rescore_{op_name.lower()}",
            f"CREATE TRIGGER trg_block_finished_rescore_{op_name.lower()} BEFORE {op_name} ON rescores "
            f"WHEN OLD.status IN {_FINISHED} BEGIN SELECT RAISE(ABORT, '{_RESCORE_MSG}'); END;",
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
                    f"WHEN (SELECT status FROM rescores WHERE id = {row}.rescore_id) IN {_FINISHED} "
                    f"BEGIN SELECT RAISE(ABORT, '{_CHILD_MSG}'); END;",
                )
            )
    return out


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in ("postgresql", "sqlite"):
        raise NotImplementedError(f"re-score migration not implemented for dialect '{dialect}'")

    op.create_table(
        "rescores",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("original_run_id", sa.String(36), nullable=False),
        sa.Column("requested_evaluators", sa.JSON(), nullable=False),
        sa.Column("evaluators", sa.JSON(), nullable=False),
        sa.Column("replaced", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("aggregates", sa.JSON(), nullable=True),
        sa.Column("code_version", sa.String(200), nullable=True),
        sa.Column("code_sha256", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["original_run_id"], ["evaluation_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status IN ('pending', 'running', 'completed', 'failed')", name="ck_rescore_status"),
    )
    op.create_index("ix_rescores_original_run_id", "rescores", ["original_run_id"])

    op.create_table(
        "rescore_results",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("rescore_id", sa.String(36), nullable=False),
        sa.Column("original_result_id", sa.String(36), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["rescore_id"], ["rescores.id"]),
        sa.ForeignKeyConstraint(["original_result_id"], ["evaluation_results.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rescore_id", "original_result_id", name="uq_rescore_result"),
    )
    op.create_index("ix_rescore_results_rescore_id", "rescore_results", ["rescore_id"])

    op.create_table(
        "rescore_metric_scores",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("rescore_id", sa.String(36), nullable=False),
        sa.Column("rescore_result_id", sa.String(36), nullable=False),
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
        sa.ForeignKeyConstraint(["rescore_id"], ["rescores.id"]),
        sa.ForeignKeyConstraint(["rescore_result_id"], ["rescore_results.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rescore_metric_scores_rescore_id", "rescore_metric_scores", ["rescore_id"])
    op.create_index("ix_rescore_metric_scores_rescore_result_id", "rescore_metric_scores", ["rescore_result_id"])

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
    op.drop_index("ix_rescore_metric_scores_rescore_result_id", table_name="rescore_metric_scores")
    op.drop_index("ix_rescore_metric_scores_rescore_id", table_name="rescore_metric_scores")
    op.drop_table("rescore_metric_scores")
    op.drop_index("ix_rescore_results_rescore_id", table_name="rescore_results")
    op.drop_table("rescore_results")
    op.drop_index("ix_rescores_original_run_id", table_name="rescores")
    op.drop_table("rescores")
