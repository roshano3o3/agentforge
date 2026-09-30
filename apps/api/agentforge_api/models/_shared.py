"""Small helpers shared by model modules."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)
