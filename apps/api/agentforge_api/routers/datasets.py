from __future__ import annotations

from agentforge_core.schemas import DatasetCreate, DatasetOut, DatasetVersionOut, DatasetVersionPublishRequest
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agentforge_api.db.base import get_session
from agentforge_api.models.dataset import Dataset, DatasetVersion, TestCase

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.post("", response_model=DatasetOut)
async def upsert_dataset(payload: DatasetCreate, session: AsyncSession = Depends(get_session)) -> Dataset:
    existing = await session.scalar(select(Dataset).where(Dataset.name == payload.name))
    if existing:
        return existing
    row = Dataset(name=payload.name, description=payload.description)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@router.post("/{dataset_id}/versions", response_model=DatasetVersionOut)
async def publish_dataset_version(
    dataset_id: str,
    payload: DatasetVersionPublishRequest,
    session: AsyncSession = Depends(get_session),
) -> DatasetVersion:
    """Always creates a brand new, immutable DatasetVersion. There is no
    update/delete route for a DatasetVersion or its TestCases — republishing
    the same logical dataset just produces the next version number.
    """
    dataset = await session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail=f"dataset '{dataset_id}' not found")

    latest_version = await session.scalar(
        select(func.max(DatasetVersion.version)).where(DatasetVersion.dataset_id == dataset_id)
    )
    next_version = (latest_version or 0) + 1

    version_row = DatasetVersion(dataset_id=dataset_id, version=next_version)
    session.add(version_row)
    await session.flush()  # assign version_row.id before we attach test cases

    for tc in payload.test_cases:
        session.add(
            TestCase(
                dataset_version_id=version_row.id,
                case_key=tc.case_key,
                input=tc.input,
                expected_answer=tc.expected_answer,
                expected_context=tc.expected_context,
                tags=tc.tags,
            )
        )

    await session.commit()

    result = await session.scalar(
        select(DatasetVersion)
        .options(selectinload(DatasetVersion.test_cases))
        .where(DatasetVersion.id == version_row.id)
    )
    assert result is not None
    return result


@router.get("/{name}/versions/{version}", response_model=DatasetVersionOut)
async def get_dataset_version(
    name: str, version: str, session: AsyncSession = Depends(get_session)
) -> DatasetVersion:
    dataset = await session.scalar(select(Dataset).where(Dataset.name == name))
    if not dataset:
        raise HTTPException(status_code=404, detail=f"dataset '{name}' not found")

    query = select(DatasetVersion).options(selectinload(DatasetVersion.test_cases)).where(
        DatasetVersion.dataset_id == dataset.id
    )
    if version == "latest":
        query = query.order_by(DatasetVersion.version.desc()).limit(1)
    else:
        try:
            version_int = int(version)
        except ValueError:
            raise HTTPException(status_code=400, detail="version must be an integer or 'latest'")
        query = query.where(DatasetVersion.version == version_int)

    row = await session.scalar(query)
    if not row:
        raise HTTPException(status_code=404, detail=f"dataset version '{version}' not found for dataset '{name}'")
    return row
