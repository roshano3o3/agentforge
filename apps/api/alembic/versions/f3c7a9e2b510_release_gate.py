"""release gate: baselines (pointers) and immutable release decisions

Revision ID: f3c7a9e2b510
Revises: e8b4c2d61a9f
Create Date: 2026-10-01 10:00:00

* baselines: (application_id, environment) -> run_id, unique per pair. A
  pointer only; setting one never copies results. A trigger refuses a
  pointer to a run that isn't `completed` or belongs to another application.
* release_decisions: one row per gate evaluation (candidate, baseline, the
  policy applied, every check, the verdict). A trigger blocks every UPDATE
  and DELETE: a decision is final once written.

New tables only; no existing table is touched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3c7a9e2b510"
down_revision: str | None = "e8b4c2d61a9f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DECISION_MSG = "release decisions are immutable"
_BASELINE_MSG = "a baseline must point to a completed run of the same application"

_PG_UP = [
    f"""
    CREATE OR REPLACE FUNCTION agentforge_block_release_decision_mutation()
    RETURNS TRIGGER AS $$
    BEGIN
        RAISE EXCEPTION '{_DECISION_MSG} (release decision %)', OLD.id USING ERRCODE = '23000';
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    CREATE TRIGGER trg_block_release_decision_mutation
    BEFORE UPDATE OR DELETE ON release_decisions
    FOR EACH ROW EXECUTE FUNCTION agentforge_block_release_decision_mutation();
    """,
    f"""
    CREATE OR REPLACE FUNCTION agentforge_check_baseline_run()
    RETURNS TRIGGER AS $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM evaluation_runs
            WHERE id = NEW.run_id AND status::text = 'completed' AND application_id = NEW.application_id
        ) THEN
            RAISE EXCEPTION '{_BASELINE_MSG}' USING ERRCODE = '23000';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    CREATE TRIGGER trg_check_baseline_run
    BEFORE INSERT OR UPDATE ON baselines
    FOR EACH ROW EXECUTE FUNCTION agentforge_check_baseline_run();
    """,
]
_PG_DOWN = [
    "DROP TRIGGER IF EXISTS trg_check_baseline_run ON baselines;",
    "DROP FUNCTION IF EXISTS agentforge_check_baseline_run();",
    "DROP TRIGGER IF EXISTS trg_block_release_decision_mutation ON release_decisions;",
    "DROP FUNCTION IF EXISTS agentforge_block_release_decision_mutation();",
]

_BASELINE_OK = (
    "EXISTS (SELECT 1 FROM evaluation_runs WHERE id = NEW.run_id AND status = 'completed' "
    "AND application_id = NEW.application_id)"
)
_SQLITE_UP = [
    f"CREATE TRIGGER trg_block_release_decision_update BEFORE UPDATE ON release_decisions "
    f"BEGIN SELECT RAISE(ABORT, '{_DECISION_MSG}'); END;",
    f"CREATE TRIGGER trg_block_release_decision_delete BEFORE DELETE ON release_decisions "
    f"BEGIN SELECT RAISE(ABORT, '{_DECISION_MSG}'); END;",
    f"CREATE TRIGGER trg_check_baseline_insert BEFORE INSERT ON baselines WHEN NOT {_BASELINE_OK} "
    f"BEGIN SELECT RAISE(ABORT, '{_BASELINE_MSG}'); END;",
    f"CREATE TRIGGER trg_check_baseline_update BEFORE UPDATE ON baselines WHEN NOT {_BASELINE_OK} "
    f"BEGIN SELECT RAISE(ABORT, '{_BASELINE_MSG}'); END;",
]
_SQLITE_DOWN = [
    "DROP TRIGGER IF EXISTS trg_check_baseline_update;",
    "DROP TRIGGER IF EXISTS trg_check_baseline_insert;",
    "DROP TRIGGER IF EXISTS trg_block_release_decision_delete;",
    "DROP TRIGGER IF EXISTS trg_block_release_decision_update;",
]


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in ("postgresql", "sqlite"):
        raise NotImplementedError(f"release gate migration not implemented for dialect '{dialect}'")

    op.create_table(
        "baselines",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("application_id", sa.String(36), nullable=False),
        sa.Column("environment", sa.String(50), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["evaluation_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("application_id", "environment", name="uq_baseline_app_env"),
    )
    op.create_table(
        "release_decisions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("application_id", sa.String(36), nullable=False),
        sa.Column("candidate_run_id", sa.String(36), nullable=False),
        sa.Column("baseline_run_id", sa.String(36), nullable=False),
        sa.Column("baseline_ref", sa.String(100), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("regression", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
        sa.ForeignKeyConstraint(["candidate_run_id"], ["evaluation_runs.id"]),
        sa.ForeignKeyConstraint(["baseline_run_id"], ["evaluation_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_release_decisions_candidate_run_id", "release_decisions", ["candidate_run_id"])

    for stmt in _PG_UP if dialect == "postgresql" else _SQLITE_UP:
        op.execute(stmt)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    for stmt in _PG_DOWN if dialect == "postgresql" else _SQLITE_DOWN:
        op.execute(stmt)
    # Dropping release_decisions discards recorded gate decisions.
    op.drop_index("ix_release_decisions_candidate_run_id", table_name="release_decisions")
    op.drop_table("release_decisions")
    op.drop_table("baselines")
