"""per-case evaluator config

Revision ID: d7a3e5c19f2b
Revises: c4e2a91d7b3f
Create Date: 2026-10-01 10:00:00

* dataset_versions.default_evaluators and test_cases.evaluators (JSON,
  nullable). Added columns only: no existing row is rewritten, so published
  versions keep exactly the content they were published with. Versions
  written before this have NULL config, which the worker reads as "every
  evaluator the run pinned" -- the pre-existing behavior.
* The published-version guard now freezes the whole version row, not just
  its status: once published, status, default_evaluators, version number,
  dataset and published_at can't change (the evaluator config is part of
  what a run of that version means).

Native ALTER TABLE only, as in c4e2a91d7b3f (a SQLite batch rebuild would
drop the dataset immutability triggers).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7a3e5c19f2b"
down_revision: str | None = "c4e2a91d7b3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PG_FREEZE_VERSION = """
CREATE OR REPLACE FUNCTION agentforge_block_unpublish()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status = 'published' AND (
        NEW.status <> 'published'
        OR NEW.default_evaluators::text IS DISTINCT FROM OLD.default_evaluators::text
        OR NEW.version <> OLD.version
        OR NEW.dataset_id <> OLD.dataset_id
        OR NEW.published_at IS DISTINCT FROM OLD.published_at
    ) THEN
        RAISE EXCEPTION 'a published dataset_version is immutable (status, evaluator config, identity)'
            USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_PG_UNPUBLISH_ONLY = """
CREATE OR REPLACE FUNCTION agentforge_block_unpublish()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status = 'published' AND NEW.status <> 'published' THEN
        RAISE EXCEPTION 'a published dataset_version can never move back to draft'
            USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

# SQLite: keep b17cf04aecaf's un-publish trigger and add one for the rest.
_SQLITE_FREEZE_VERSION = """
CREATE TRIGGER trg_block_published_version_update
BEFORE UPDATE ON dataset_versions
WHEN OLD.status = 'published' AND (
    NEW.default_evaluators IS NOT OLD.default_evaluators
    OR NEW.version IS NOT OLD.version
    OR NEW.dataset_id IS NOT OLD.dataset_id
    OR NEW.published_at IS NOT OLD.published_at
)
BEGIN
    SELECT RAISE(ABORT, 'a published dataset_version is immutable (status, evaluator config, identity)');
END;
"""


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.add_column("dataset_versions", sa.Column("default_evaluators", sa.JSON(), nullable=True))
    op.add_column("test_cases", sa.Column("evaluators", sa.JSON(), nullable=True))
    if dialect == "postgresql":
        op.execute(_PG_FREEZE_VERSION)
    elif dialect == "sqlite":
        op.execute(_SQLITE_FREEZE_VERSION)
    else:
        raise NotImplementedError(f"per-case evaluator config migration not implemented for dialect '{dialect}'")


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(_PG_UNPUBLISH_ONLY)
    else:
        op.execute("DROP TRIGGER IF EXISTS trg_block_published_version_update;")
    op.drop_column("test_cases", "evaluators")
    op.drop_column("dataset_versions", "default_evaluators")
