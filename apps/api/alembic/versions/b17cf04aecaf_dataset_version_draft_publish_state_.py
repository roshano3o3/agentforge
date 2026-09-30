"""dataset version draft/publish state machine

Revision ID: b17cf04aecaf
Revises: 8fdb390e1f2b
Create Date: 2026-09-29 14:30:52.989671

Adds DatasetVersion.status (draft/published) and makes published_at
nullable (only set once a version is actually published). Every
pre-existing row was created under the old model, where a version was
published the instant it was inserted -- so they're backfilled to
status='published' with created_at copied from their existing
published_at (accurate, since those two timestamps really were the same
instant for every row created before this migration).

Also adds a DB-level guard, independent of the API/service layer: once a
dataset_version is published, its test_cases can no longer be inserted,
updated, or deleted, and its status can never move back to draft. This is
implemented as a real trigger on both supported engines (a PL/pgSQL
trigger function on Postgres, three per-operation triggers on SQLite,
since SQLite triggers are one-operation-each) -- not just documented, since
"a documented equivalent for SQLite" was interpreted as "the same
enforcement, expressed in SQLite's trigger syntax," not as a comment.

Each statement is executed individually (`op.execute()` per statement):
neither the sqlite3 DBAPI nor asyncpg's extended query protocol reliably
accept multiple semicolon-separated statements in a single execute call.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b17cf04aecaf'
down_revision: Union[str, None] = '8fdb390e1f2b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PG_UP_STATEMENTS = [
    """
    CREATE OR REPLACE FUNCTION agentforge_block_published_test_case_mutation()
    RETURNS TRIGGER AS $$
    DECLARE
        v_status TEXT;
    BEGIN
        SELECT status INTO v_status FROM dataset_versions
        WHERE id = COALESCE(NEW.dataset_version_id, OLD.dataset_version_id);
        IF v_status = 'published' THEN
            RAISE EXCEPTION 'test_cases are immutable once their dataset_version is published'
                USING ERRCODE = '23000';
        END IF;
        RETURN COALESCE(NEW, OLD);
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    CREATE TRIGGER trg_block_published_test_case_mutation
    BEFORE INSERT OR UPDATE OR DELETE ON test_cases
    FOR EACH ROW EXECUTE FUNCTION agentforge_block_published_test_case_mutation();
    """,
    """
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
    """,
    """
    CREATE TRIGGER trg_block_unpublish
    BEFORE UPDATE ON dataset_versions
    FOR EACH ROW EXECUTE FUNCTION agentforge_block_unpublish();
    """,
]

_PG_DOWN_STATEMENTS = [
    "DROP TRIGGER IF EXISTS trg_block_unpublish ON dataset_versions;",
    "DROP FUNCTION IF EXISTS agentforge_block_unpublish();",
    "DROP TRIGGER IF EXISTS trg_block_published_test_case_mutation ON test_cases;",
    "DROP FUNCTION IF EXISTS agentforge_block_published_test_case_mutation();",
]

_SQLITE_UP_STATEMENTS = [
    """
    CREATE TRIGGER trg_block_published_test_case_insert
    BEFORE INSERT ON test_cases
    WHEN (SELECT status FROM dataset_versions WHERE id = NEW.dataset_version_id) = 'published'
    BEGIN
        SELECT RAISE(ABORT, 'test_cases are immutable once their dataset_version is published');
    END;
    """,
    """
    CREATE TRIGGER trg_block_published_test_case_update
    BEFORE UPDATE ON test_cases
    WHEN (SELECT status FROM dataset_versions WHERE id = OLD.dataset_version_id) = 'published'
    BEGIN
        SELECT RAISE(ABORT, 'test_cases are immutable once their dataset_version is published');
    END;
    """,
    """
    CREATE TRIGGER trg_block_published_test_case_delete
    BEFORE DELETE ON test_cases
    WHEN (SELECT status FROM dataset_versions WHERE id = OLD.dataset_version_id) = 'published'
    BEGIN
        SELECT RAISE(ABORT, 'test_cases are immutable once their dataset_version is published');
    END;
    """,
    """
    CREATE TRIGGER trg_block_unpublish
    BEFORE UPDATE ON dataset_versions
    WHEN OLD.status = 'published' AND NEW.status != 'published'
    BEGIN
        SELECT RAISE(ABORT, 'a published dataset_version can never move back to draft');
    END;
    """,
]

_SQLITE_DOWN_STATEMENTS = [
    "DROP TRIGGER IF EXISTS trg_block_unpublish;",
    "DROP TRIGGER IF EXISTS trg_block_published_test_case_delete;",
    "DROP TRIGGER IF EXISTS trg_block_published_test_case_update;",
    "DROP TRIGGER IF EXISTS trg_block_published_test_case_insert;",
]


_STATUS_ENUM = sa.Enum("draft", "published", name="datasetversionstatus")


def upgrade() -> None:
    # op.add_column() does not emit CREATE TYPE for a new Enum (only
    # create_table does), so on Postgres the type must be created explicitly
    # or the ALTER TABLE fails with 'type "datasetversionstatus" does not
    # exist'. No-op on SQLite, which has no named enum types -- which is why
    # this went unnoticed until the migration was first run on Postgres.
    _STATUS_ENUM.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "dataset_versions",
        sa.Column(
            "status",
            _STATUS_ENUM,
            nullable=False,
            server_default="published",
        ),
    )
    op.add_column("dataset_versions", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE dataset_versions SET created_at = published_at WHERE created_at IS NULL")

    with op.batch_alter_table("dataset_versions") as batch_op:
        batch_op.alter_column("created_at", existing_type=sa.DateTime(timezone=True), nullable=False)
        batch_op.alter_column("published_at", existing_type=sa.DateTime(timezone=True), nullable=True)

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        for stmt in _PG_UP_STATEMENTS:
            op.execute(stmt)
    elif dialect == "sqlite":
        for stmt in _SQLITE_UP_STATEMENTS:
            op.execute(stmt)
    else:
        raise NotImplementedError(
            f"no dataset_version immutability trigger implemented for dialect '{dialect}'"
        )


def downgrade() -> None:
    # The pre-draft schema requires published_at on every row, so a draft can't
    # be represented there. Refuse clearly rather than silently deleting drafts
    # (or failing later with an opaque NOT NULL violation).
    drafts = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM dataset_versions WHERE status = 'draft'")
    ).scalar_one()
    if drafts:
        raise RuntimeError(
            f"cannot downgrade: {drafts} draft dataset_version(s) exist, and the previous "
            "schema has no way to represent a draft. Publish or delete them first."
        )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        for stmt in _PG_DOWN_STATEMENTS:
            op.execute(stmt)
    elif dialect == "sqlite":
        for stmt in _SQLITE_DOWN_STATEMENTS:
            op.execute(stmt)

    with op.batch_alter_table("dataset_versions") as batch_op:
        batch_op.alter_column("published_at", existing_type=sa.DateTime(timezone=True), nullable=False)
        batch_op.alter_column("created_at", existing_type=sa.DateTime(timezone=True), nullable=True)

    op.drop_column("dataset_versions", "created_at")
    op.drop_column("dataset_versions", "status")
    _STATUS_ENUM.drop(op.get_bind(), checkfirst=True)
