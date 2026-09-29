from __future__ import annotations

from agentforge_core.schemas import (
    ApplicationCreate,
    ApplicationOut,
    ApplicationVersionCreate,
    ApplicationVersionOut,
)
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentforge_api.db.base import get_session
from agentforge_api.models.application import Application, ApplicationVersion

router = APIRouter(prefix="/applications", tags=["applications"])


@router.post("", response_model=ApplicationOut)
async def upsert_application(
    payload: ApplicationCreate, session: AsyncSession = Depends(get_session)
) -> Application:
    """Get-or-create by name. Idempotent so the CLI can call this on every
    `evaluate` invocation without needing a separate "does it exist" check.
    """
    existing = await session.scalar(select(Application).where(Application.name == payload.name))
    if existing:
        return existing
    row = Application(name=payload.name, description=payload.description)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@router.get("/{name}", response_model=ApplicationOut)
async def get_application(name: str, session: AsyncSession = Depends(get_session)) -> Application:
    row = await session.scalar(select(Application).where(Application.name == name))
    if not row:
        raise HTTPException(status_code=404, detail=f"application '{name}' not found")
    return row


@router.post("/{application_id}/versions", response_model=ApplicationVersionOut)
async def upsert_application_version(
    application_id: str,
    payload: ApplicationVersionCreate,
    session: AsyncSession = Depends(get_session),
) -> ApplicationVersion:
    app_row = await session.get(Application, application_id)
    if not app_row:
        raise HTTPException(status_code=404, detail=f"application '{application_id}' not found")

    existing = await session.scalar(
        select(ApplicationVersion).where(
            ApplicationVersion.application_id == application_id,
            ApplicationVersion.version == payload.version,
        )
    )
    if existing:
        return existing

    row = ApplicationVersion(
        application_id=application_id, version=payload.version, description=payload.description
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row
