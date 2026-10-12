"""adversarial testing: per-case scenario (sent to the adapter) and safety block (evaluators only)

Revision ID: a5d81c3f9e27
Revises: f3c7a9e2b510
Create Date: 2026-10-01 16:00:00

* test_cases.scenario: how the application's environment is set up for the
  case -- mock tool failures, text merged into a tool's result, the session
  user's permissions (agentforge_core.scenario). The worker passes it to the
  adapter.
* test_cases.safety: the attack category, the source case it was derived
  from, and the safety evaluators' expectations (agentforge_evaluators.safety).
  Never sent to the adapter.

Added columns only: existing rows aren't rewritten (both NULL), and the
published-version test_cases trigger from b17cf04aecaf already freezes every
column of a published case, these included. Native ALTER TABLE, as in
e8b4c2d61a9f (a SQLite batch rebuild would drop that trigger).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a5d81c3f9e27"
down_revision: str | None = "f3c7a9e2b510"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("test_cases", sa.Column("scenario", sa.JSON(), nullable=True))
    op.add_column("test_cases", sa.Column("safety", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("test_cases", "safety")
    op.drop_column("test_cases", "scenario")
