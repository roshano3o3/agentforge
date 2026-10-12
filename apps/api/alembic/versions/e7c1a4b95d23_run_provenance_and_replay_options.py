"""provenance (code version, source and pricing hashes) on runs and replays; runs' replay options

Revision ID: e7c1a4b95d23
Revises: d4a8f2c61e57
Create Date: 2026-10-06 18:00:00

* evaluation_runs: code_version, code_sha256, pricing_sha256 (what the worker
  ran with) and replay_options (the adapter's declared replay settings).
* replays: code_version, code_sha256, pricing_sha256.

Added nullable columns only (native ALTER TABLE ADD COLUMN; existing rows and
the immutability triggers are untouched). Existing runs keep NULL: "not recorded".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7c1a4b95d23"
down_revision: str | None = "d4a8f2c61e57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROVENANCE = (("code_version", 200), ("code_sha256", 64), ("pricing_sha256", 64))


def upgrade() -> None:
    for table in ("evaluation_runs", "replays"):
        for name, length in _PROVENANCE:
            op.add_column(table, sa.Column(name, sa.String(length), nullable=True))
    op.add_column("evaluation_runs", sa.Column("replay_options", sa.JSON(), nullable=True))


def downgrade() -> None:
    # SQLite >= 3.35 drops columns natively; no batch rebuild (it would drop the triggers).
    op.drop_column("evaluation_runs", "replay_options")
    for table in ("replays", "evaluation_runs"):
        for name, _length in reversed(_PROVENANCE):
            op.drop_column(table, name)
