"""baselines keyed by (application, environment, dataset)

Revision ID: b9e4f1c27d36
Revises: a5d81c3f9e27
Create Date: 2026-10-02 09:00:00

A baseline pointer was one run per (application, environment), so
`production` couldn't name one run for the trajectory dataset and another
for the safety dataset. Now it's one per (application, environment,
dataset), where the dataset is the run's own.

* baselines.dataset_id, backfilled from each pointer's run;
* the unique key (application_id, environment) becomes
  (application_id, environment, dataset_id);
* the pointer trigger additionally requires dataset_id to be the run's dataset.

SQLite can't drop a table constraint in place, so there the table is rebuilt
(batch mode) and its two triggers are recreated afterwards: a rebuild drops a
table's triggers, which is why earlier migrations avoided batch mode.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b9e4f1c27d36"
down_revision: str | None = "a5d81c3f9e27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MSG = "a baseline must point to a completed run of the same application and dataset"
_OLD_MSG = "a baseline must point to a completed run of the same application"
_RUN_DATASET = (
    "(SELECT dv.dataset_id FROM evaluation_runs r JOIN dataset_versions dv ON dv.id = r.dataset_version_id "
    "WHERE r.id = {row}.run_id)"
)
_BACKFILL = f"UPDATE baselines SET dataset_id = {_RUN_DATASET.format(row='baselines')}"


def _pg_function(with_dataset: bool) -> str:
    dataset = (
        "AND dataset_version_id IN (SELECT id FROM dataset_versions WHERE dataset_id = NEW.dataset_id)"
        if with_dataset
        else ""
    )
    return f"""
    CREATE OR REPLACE FUNCTION agentforge_check_baseline_run()
    RETURNS TRIGGER AS $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM evaluation_runs
            WHERE id = NEW.run_id AND status::text = 'completed' AND application_id = NEW.application_id
            {dataset}
        ) THEN
            RAISE EXCEPTION '{_MSG if with_dataset else _OLD_MSG}' USING ERRCODE = '23000';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """


def _sqlite_triggers(with_dataset: bool) -> list[str]:
    ok = (
        "EXISTS (SELECT 1 FROM evaluation_runs WHERE id = NEW.run_id AND status = 'completed' "
        "AND application_id = NEW.application_id"
        + (
            " AND dataset_version_id IN (SELECT id FROM dataset_versions WHERE dataset_id = NEW.dataset_id)"
            if with_dataset
            else ""
        )
        + ")"
    )
    msg = _MSG if with_dataset else _OLD_MSG
    return [
        "DROP TRIGGER IF EXISTS trg_check_baseline_insert;",
        "DROP TRIGGER IF EXISTS trg_check_baseline_update;",
        f"CREATE TRIGGER trg_check_baseline_insert BEFORE INSERT ON baselines WHEN NOT {ok} "
        f"BEGIN SELECT RAISE(ABORT, '{msg}'); END;",
        f"CREATE TRIGGER trg_check_baseline_update BEFORE UPDATE ON baselines WHEN NOT {ok} "
        f"BEGIN SELECT RAISE(ABORT, '{msg}'); END;",
    ]


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in ("postgresql", "sqlite"):
        raise NotImplementedError(f"baselines-per-dataset migration not implemented for dialect '{dialect}'")
    if dialect == "postgresql":
        op.add_column("baselines", sa.Column("dataset_id", sa.String(36), nullable=True))
        op.execute(_BACKFILL)
        op.alter_column("baselines", "dataset_id", nullable=False)
        op.create_foreign_key("fk_baselines_dataset", "baselines", "datasets", ["dataset_id"], ["id"])
        op.drop_constraint("uq_baseline_app_env", "baselines", type_="unique")
        op.create_unique_constraint(
            "uq_baseline_app_env_dataset", "baselines", ["application_id", "environment", "dataset_id"]
        )
        op.execute(_pg_function(with_dataset=True))
        return
    op.add_column("baselines", sa.Column("dataset_id", sa.String(36), nullable=True))
    op.execute(_BACKFILL)
    with op.batch_alter_table("baselines", recreate="always") as batch:
        batch.alter_column("dataset_id", existing_type=sa.String(36), nullable=False)
        batch.create_foreign_key("fk_baselines_dataset", "datasets", ["dataset_id"], ["id"])
        batch.drop_constraint("uq_baseline_app_env", type_="unique")
        batch.create_unique_constraint("uq_baseline_app_env_dataset", ["application_id", "environment", "dataset_id"])
    for stmt in _sqlite_triggers(with_dataset=True):
        op.execute(stmt)


def downgrade() -> None:
    # With more than one pointer per (application, environment), the old key
    # can't hold them: keep the most recently set one.
    op.execute(
        "DELETE FROM baselines WHERE id NOT IN ("
        "SELECT id FROM baselines b WHERE b.set_at = (SELECT MAX(b2.set_at) FROM baselines b2 "
        "WHERE b2.application_id = b.application_id AND b2.environment = b.environment))"
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(_pg_function(with_dataset=False))
        op.drop_constraint("uq_baseline_app_env_dataset", "baselines", type_="unique")
        op.create_unique_constraint("uq_baseline_app_env", "baselines", ["application_id", "environment"])
        op.drop_constraint("fk_baselines_dataset", "baselines", type_="foreignkey")
        op.drop_column("baselines", "dataset_id")
        return
    with op.batch_alter_table("baselines", recreate="always") as batch:
        batch.drop_constraint("uq_baseline_app_env_dataset", type_="unique")
        batch.create_unique_constraint("uq_baseline_app_env", ["application_id", "environment"])
        batch.drop_constraint("fk_baselines_dataset", type_="foreignkey")
        batch.drop_column("dataset_id")
    for stmt in _sqlite_triggers(with_dataset=False):
        op.execute(stmt)
