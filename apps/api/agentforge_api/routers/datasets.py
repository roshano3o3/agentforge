from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agentforge_api.db.base import get_session
from agentforge_api.models.dataset import Dataset, DatasetVersion, DatasetVersionStatus, TestCase
from agentforge_core.schemas import DatasetCreate, DatasetOut, DatasetVersionOut, DatasetVersionTestCasesRequest
from agentforge_evaluators import EvaluatorConfigError, validate_config

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.get("", response_model=list[DatasetOut])
async def list_datasets(session: AsyncSession = Depends(get_session)) -> list[Dataset]:
    rows = await session.scalars(select(Dataset).order_by(Dataset.created_at.desc()))
    return list(rows)


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


_CASE_FIELDS = (
    "input",
    "expected_answer",
    "expected_context",
    "tags",
    "evaluators",
)


def _case_values(source: object) -> dict:
    """The writable content of a test case (a TestCaseIn or a TestCase row).
    One list, so create / PATCH / new-draft can't drift on which fields they carry."""
    return {name: getattr(source, name) for name in _CASE_FIELDS}


def _validated(payload: DatasetVersionTestCasesRequest) -> DatasetVersionTestCasesRequest:
    """Check every evaluator config against the registry (names, versions,
    params) before anything is written. 422 names the case and the problem."""
    try:
        payload.default_evaluators = validate_config(payload.default_evaluators, allow_disable=False)
        for tc in payload.test_cases:
            try:
                tc.evaluators = validate_config(tc.evaluators)
            except EvaluatorConfigError as exc:
                raise EvaluatorConfigError(f"test case '{tc.case_key}': {exc}") from exc
    except EvaluatorConfigError as exc:
        raise HTTPException(status_code=422, detail=f"invalid evaluator config: {exc}") from exc
    return payload


def _carried_over_config(tc: TestCase) -> dict | None:
    """A copied case's config, with Phase 2 legacy answer assertions turned
    into explicit params. Only used for new drafts: published rows are never
    rewritten, and the worker maps legacy fields at run time instead."""
    config = dict(tc.evaluators) if tc.evaluators is not None else None
    legacy: dict = {}
    if tc.expected_answer_contains:
        legacy["answer_contains"] = {"phrases": list(tc.expected_answer_contains)}
    if tc.expected_answer_regex:
        legacy["answer_regex"] = {"pattern": tc.expected_answer_regex}
    if not legacy:
        return config
    config = config or {}
    configured = {key.partition("@")[0] for key in config}
    for name, params in legacy.items():
        if name not in configured:
            config[name] = params
    return config


async def _load_version_with_cases(version_id: str, session: AsyncSession) -> DatasetVersion:
    row = await session.scalar(
        select(DatasetVersion).options(selectinload(DatasetVersion.test_cases)).where(DatasetVersion.id == version_id)
    )
    assert row is not None
    return row


async def _apply_test_cases(version_id: str, payload: DatasetVersionTestCasesRequest, session: AsyncSession) -> None:
    """Replace the version's test cases with exactly `payload.test_cases`,
    diffed by case_key: matching keys are updated in place, missing keys are
    deleted, new keys are inserted. Caller is responsible for having already
    verified the version is a draft.

    Deliberately queries for the existing rows directly (rather than via
    `dataset_version.test_cases`, the relationship) so it never populates
    that relationship on an object the caller might re-fetch later in the
    same request -- with `expire_on_commit=False` (see db/base.py), an
    already-loaded collection is NOT refreshed by a later selectinload
    query against the same identity-mapped object, so touching the
    relationship here would hand the caller stale, pre-mutation data.
    """
    existing_rows = list(await session.scalars(select(TestCase).where(TestCase.dataset_version_id == version_id)))
    existing_by_key = {tc.case_key: tc for tc in existing_rows}
    incoming_keys = {tc.case_key for tc in payload.test_cases}

    for case_key, stale_tc in existing_by_key.items():
        if case_key not in incoming_keys:
            await session.delete(stale_tc)

    for tc in payload.test_cases:
        existing_tc = existing_by_key.get(tc.case_key)
        if existing_tc is not None:
            for name, value in _case_values(tc).items():
                setattr(existing_tc, name, value)
        else:
            session.add(TestCase(dataset_version_id=version_id, case_key=tc.case_key, **_case_values(tc)))


@router.post("/{dataset_id}/versions", response_model=DatasetVersionOut)
async def create_draft_version(
    dataset_id: str,
    payload: DatasetVersionTestCasesRequest,
    session: AsyncSession = Depends(get_session),
) -> DatasetVersion:
    """Creates a new DRAFT version (optionally pre-populated with test
    cases). Always the next sequential version number for this dataset,
    regardless of whether earlier drafts were ever published.
    """
    dataset = await session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail=f"dataset '{dataset_id}' not found")
    payload = _validated(payload)

    latest_version = await session.scalar(
        select(func.max(DatasetVersion.version)).where(DatasetVersion.dataset_id == dataset_id)
    )
    next_version = (latest_version or 0) + 1

    version_row = DatasetVersion(
        dataset_id=dataset_id,
        version=next_version,
        status=DatasetVersionStatus.draft,
        default_evaluators=payload.default_evaluators,
    )
    session.add(version_row)
    await session.flush()

    for tc in payload.test_cases:
        session.add(TestCase(dataset_version_id=version_row.id, case_key=tc.case_key, **_case_values(tc)))

    await session.commit()
    return await _load_version_with_cases(version_row.id, session)


async def _get_version_or_404(
    name: str, version: str, session: AsyncSession, *, eager_test_cases: bool = True
) -> DatasetVersion:
    """`eager_test_cases=False` is for callers that only need the version's
    own columns (status, version number) and will not read `.test_cases`.
    Skipping the eager load matters when the caller is later going to
    mutate test cases and re-fetch this same version in the same session:
    with `expire_on_commit=False` (see db/base.py), a relationship that was
    already populated here would NOT be refreshed by a later selectinload
    query against the same identity-mapped object -- it would hand back
    stale, pre-mutation data. Not loading it here at all sidesteps that
    entirely (see _apply_test_cases's docstring for the full story).
    """
    dataset = await session.scalar(select(Dataset).where(Dataset.name == name))
    if not dataset:
        raise HTTPException(status_code=404, detail=f"dataset '{name}' not found")

    query = select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id)
    if eager_test_cases:
        query = query.options(selectinload(DatasetVersion.test_cases))
    if version == "latest":
        # "latest" means latest PUBLISHED version -- evaluating against a
        # draft would break the reproducibility guarantee that a run's
        # dataset_version never changes underneath it.
        query = (
            query.where(DatasetVersion.status == DatasetVersionStatus.published)
            .order_by(DatasetVersion.version.desc())
            .limit(1)
        )
    else:
        try:
            version_int = int(version)
        except ValueError:
            raise HTTPException(status_code=400, detail="version must be an integer or 'latest'") from None
        query = query.where(DatasetVersion.version == version_int)

    row = await session.scalar(query)
    if not row:
        detail = (
            f"no published version found for dataset '{name}' (there may be unpublished drafts)"
            if version == "latest"
            else f"dataset version '{version}' not found for dataset '{name}'"
        )
        raise HTTPException(status_code=404, detail=detail)
    return row


@router.patch("/{name}/versions/{version}", response_model=DatasetVersionOut)
async def edit_draft_version(
    name: str,
    version: str,
    payload: DatasetVersionTestCasesRequest,
    session: AsyncSession = Depends(get_session),
) -> DatasetVersion:
    """Replace a draft version's test cases. Same route rejects with 409 if
    the version is published -- this is the one code path that decides
    editable-or-not, so the check can't be bypassed by hitting a different
    endpoint. (There is also a DB-level trigger enforcing the same rule
    independent of this code -- see the dataset_version_immutability
    migration.)
    """
    version_row = await _get_version_or_404(name, version, session, eager_test_cases=False)
    if version_row.status != DatasetVersionStatus.draft:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Dataset version '{name}' v{version_row.version} is published and immutable. "
                "Use POST /datasets/{name}/versions/{version}/new-draft to start a new "
                "version copied from this one instead."
            ),
        )
    # Only touched when sent: a client editing test cases (e.g. the dashboard)
    # must not wipe the version's default config by omitting it. Checked
    # before _validated(), whose normalizing assignment would mark the field
    # as set.
    default_sent = "default_evaluators" in payload.model_fields_set
    payload = _validated(payload)
    if default_sent:
        version_row.default_evaluators = payload.default_evaluators
    await _apply_test_cases(version_row.id, payload, session)
    await session.commit()
    return await _load_version_with_cases(version_row.id, session)


@router.post("/{name}/versions/{version}/publish", response_model=DatasetVersionOut)
async def publish_version(name: str, version: str, session: AsyncSession = Depends(get_session)) -> DatasetVersion:
    """Freezes a draft: one-way transition to `published`. Never reversed by
    any route.
    """
    version_row = await _get_version_or_404(name, version, session)
    if version_row.status != DatasetVersionStatus.draft:
        raise HTTPException(
            status_code=409, detail=f"Dataset version '{name}' v{version_row.version} is already published."
        )
    if not version_row.test_cases:
        raise HTTPException(status_code=400, detail="cannot publish a version with zero test cases")

    version_row.status = DatasetVersionStatus.published
    version_row.published_at = datetime.now(UTC)
    await session.commit()
    return await _load_version_with_cases(version_row.id, session)


@router.post("/{name}/versions/{version}/new-draft", response_model=DatasetVersionOut)
async def new_draft_from_version(
    name: str, version: str, session: AsyncSession = Depends(get_session)
) -> DatasetVersion:
    """Creates a new draft version (next sequential number) whose test cases
    are copied from `version` (draft or published). This is the only
    supported way to "edit" a published version's content -- as a new
    version, never in place.
    """
    source = await _get_version_or_404(name, version, session)

    latest_version = await session.scalar(
        select(func.max(DatasetVersion.version)).where(DatasetVersion.dataset_id == source.dataset_id)
    )
    next_version = (latest_version or 0) + 1

    new_row = DatasetVersion(
        dataset_id=source.dataset_id,
        version=next_version,
        status=DatasetVersionStatus.draft,
        default_evaluators=source.default_evaluators,
    )
    session.add(new_row)
    await session.flush()

    for tc in source.test_cases:
        values = {**_case_values(tc), "evaluators": _carried_over_config(tc)}
        session.add(TestCase(dataset_version_id=new_row.id, case_key=tc.case_key, **values))

    await session.commit()
    return await _load_version_with_cases(new_row.id, session)


@router.get("/{name}", response_model=DatasetOut)
async def get_dataset(name: str, session: AsyncSession = Depends(get_session)) -> Dataset:
    dataset = await session.scalar(select(Dataset).where(Dataset.name == name))
    if not dataset:
        raise HTTPException(status_code=404, detail=f"dataset '{name}' not found")
    return dataset


@router.get("/{name}/versions", response_model=list[DatasetVersionOut])
async def list_dataset_versions(name: str, session: AsyncSession = Depends(get_session)) -> list[DatasetVersion]:
    dataset = await session.scalar(select(Dataset).where(Dataset.name == name))
    if not dataset:
        raise HTTPException(status_code=404, detail=f"dataset '{name}' not found")
    rows = await session.scalars(
        select(DatasetVersion)
        .options(selectinload(DatasetVersion.test_cases))
        .where(DatasetVersion.dataset_id == dataset.id)
        .order_by(DatasetVersion.version.desc())
    )
    return list(rows)


@router.get("/{name}/versions/{version}", response_model=DatasetVersionOut)
async def get_dataset_version(name: str, version: str, session: AsyncSession = Depends(get_session)) -> DatasetVersion:
    return await _get_version_or_404(name, version, session)
