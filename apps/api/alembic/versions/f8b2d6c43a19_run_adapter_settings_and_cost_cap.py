"""run-level adapter settings and cost cap; replays carry the run's settings

Revision ID: f8b2d6c43a19
Revises: e7c1a4b95d23
Create Date: 2026-10-07 18:00:00

* evaluation_runs: adapter_settings (what every case runs with, e.g. an LLM
  adapter's provider/model/prompt) and max_cost_usd (the worker stops the run
  once its estimated spend passes it).
* replays: adapter_settings (copied from the original run; the replay's own
  overrides go on top).

Added nullable columns only (native ALTER TABLE ADD COLUMN; existing rows and
the immutability triggers are untouched). Existing runs keep NULL: none set.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8b2d6c43a19"
down_revision: str | None = "e7c1a4b95d23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("evaluation_runs", sa.Column("adapter_settings", sa.JSON(), nullable=True))
    op.add_column("evaluation_runs", sa.Column("max_cost_usd", sa.Float(), nullable=True))
    op.add_column("replays", sa.Column("adapter_settings", sa.JSON(), nullable=True))


def downgrade() -> None:
    # SQLite >= 3.35 drops columns natively; no batch rebuild (it would drop the triggers).
    op.drop_column("replays", "adapter_settings")
    op.drop_column("evaluation_runs", "max_cost_usd")
    op.drop_column("evaluation_runs", "adapter_settings")
