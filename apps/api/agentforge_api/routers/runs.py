from __future__ import annotations

from datetime import datetime, timezone

from agentforge_core.schemas import (
    EvaluationResultOut,
    EvaluationResultSubmitRequest,
    EvaluationRunCreate,
    EvaluationRunOut,
    EvaluationRunSummaryOut,
    RunCompleteRequest,
)
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agentforge_api.db.base import get_session
from agentforge_api.models.application import Application, ApplicationVersion
from agentforge_api.models.dataset import Dataset, DatasetVersion, TestCase
from agentforge_api.models.evaluation import EvaluationResult, EvaluationRun, RunStatus

router = APIRouter(prefix="/runs", tags=["runs"])


async def _load_run_or_404(run_id: str, session: AsyncSession) -> EvaluationRun:
    row = await session.scalar(
        select(EvaluationRun)
        .options(selectinload(EvaluationRun.results).selectinload(EvaluationResult.test_case))
        .where(EvaluationRun.id == run_id)
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    return row


def _aggregate(run: EvaluationRun) -> dict:
    scored = [r.score for r in run.results if r.score is not None]
    passed = [r.passed for r in run.results if r.passed is not None]
    latencies = [r.latency_ms for r in run.results]
    return {
        "case_count": len(run.results),
        "mean_score": (sum(scored) / len(scored)) if scored else None,
        "pass_rate": (sum(1 for p in passed if p) / len(passed)) if passed else None,
        "avg_latency_ms": (sum(latencies) / len(latencies)) if latencies else None,
    }


async def _to_run_out(run: EvaluationRun, session: AsyncSession) -> EvaluationRunOut:
    app = await session.get(Application, run.application_id)
    app_version = await session.get(ApplicationVersion, run.application_version_id)
    dataset_version = await session.get(DatasetVersion, run.dataset_version_id)
    assert app and app_version and dataset_version
    dataset = await session.get(Dataset, dataset_version.dataset_id)
    assert dataset

    agg = _aggregate(run)
    results_out = [
        EvaluationResultOut(
            id=r.id,
            test_case_id=r.test_case_id,
            case_key=r.test_case.case_key,
            input=r.test_case.input,
            expected_context=r.test_case.expected_context,
            retrieved_doc_ids=r.retrieved_doc_ids,
            output_answer=r.output_answer,
            score=r.score,
            passed=r.passed,
            evidence=r.evidence,
            latency_ms=r.latency_ms,
            status=r.status,
            error_message=r.error_message,
        )
        for r in run.results
    ]

    return EvaluationRunOut(
        id=run.id,
        application_id=run.application_id,
        application_name=app.name,
        application_version_id=run.application_version_id,
        application_version=app_version.version,
        dataset_version_id=run.dataset_version_id,
        dataset_name=dataset.name,
        dataset_version=dataset_version.version,
        provider_type=run.provider_type,
        evaluator_name=run.evaluator_name,
        evaluator_version=run.evaluator_version,
        environment=run.environment,
        threshold=run.threshold,
        git_commit_sha=run.git_commit_sha,
        status=run.status,
        created_at=run.created_at,
        completed_at=run.completed_at,
        results=results_out,
        **agg,
    )


@router.post("", response_model=EvaluationRunOut)
async def create_run(payload: EvaluationRunCreate, session: AsyncSession = Depends(get_session)) -> EvaluationRunOut:
    if not await session.get(Application, payload.application_id):
        raise HTTPException(status_code=404, detail=f"application '{payload.application_id}' not found")
    if not await session.get(ApplicationVersion, payload.application_version_id):
        raise HTTPException(
            status_code=404, detail=f"application version '{payload.application_version_id}' not found"
        )
    if not await session.get(DatasetVersion, payload.dataset_version_id):
        raise HTTPException(status_code=404, detail=f"dataset version '{payload.dataset_version_id}' not found")

    run = EvaluationRun(
        application_id=payload.application_id,
        application_version_id=payload.application_version_id,
        dataset_version_id=payload.dataset_version_id,
        provider_type=payload.provider_type,
        evaluator_name=payload.evaluator_name,
        evaluator_version=payload.evaluator_version,
        environment=payload.environment,
        threshold=payload.threshold,
        git_commit_sha=payload.git_commit_sha,
        status=RunStatus.running,
    )
    session.add(run)
    await session.commit()
    run = await _load_run_or_404(run.id, session)
    return await _to_run_out(run, session)


@router.post("/{run_id}/results", response_model=EvaluationRunOut)
async def submit_results(
    run_id: str, payload: EvaluationResultSubmitRequest, session: AsyncSession = Depends(get_session)
) -> EvaluationRunOut:
    run = await _load_run_or_404(run_id, session)
    if run.status != RunStatus.running:
        raise HTTPException(
            status_code=409, detail=f"run '{run_id}' is not accepting results (status={run.status.value})"
        )

    for item in payload.results:
        test_case = await session.get(TestCase, item.test_case_id)
        if not test_case or test_case.dataset_version_id != run.dataset_version_id:
            raise HTTPException(
                status_code=400,
                detail=f"test_case '{item.test_case_id}' does not belong to this run's dataset version",
            )
        session.add(
            EvaluationResult(
                evaluation_run_id=run.id,
                test_case_id=item.test_case_id,
                retrieved_doc_ids=item.retrieved_doc_ids,
                output_answer=item.output_answer,
                score=item.score,
                passed=item.passed,
                evidence=item.evidence,
                latency_ms=item.latency_ms,
                status=item.status,
                error_message=item.error_message,
            )
        )
    await session.commit()
    run = await _load_run_or_404(run_id, session)
    return await _to_run_out(run, session)


@router.post("/{run_id}/complete", response_model=EvaluationRunOut)
async def complete_run(
    run_id: str, payload: RunCompleteRequest, session: AsyncSession = Depends(get_session)
) -> EvaluationRunOut:
    run = await _load_run_or_404(run_id, session)
    if run.status != RunStatus.running:
        raise HTTPException(status_code=409, detail=f"run '{run_id}' already finalized (status={run.status.value})")

    run.status = RunStatus.completed if payload.status == "completed" else RunStatus.failed
    run.completed_at = datetime.now(timezone.utc)
    await session.commit()
    run = await _load_run_or_404(run_id, session)
    return await _to_run_out(run, session)


@router.get("/{run_id}", response_model=EvaluationRunOut)
async def get_run(run_id: str, session: AsyncSession = Depends(get_session)) -> EvaluationRunOut:
    run = await _load_run_or_404(run_id, session)
    return await _to_run_out(run, session)


@router.get("", response_model=list[EvaluationRunSummaryOut])
async def list_runs(session: AsyncSession = Depends(get_session)) -> list[EvaluationRunSummaryOut]:
    rows = await session.scalars(
        select(EvaluationRun)
        .options(selectinload(EvaluationRun.results).selectinload(EvaluationResult.test_case))
        .order_by(EvaluationRun.created_at.desc())
    )
    summaries = []
    for run in rows:
        full = await _to_run_out(run, session)
        summaries.append(
            EvaluationRunSummaryOut(
                id=full.id,
                application_name=full.application_name,
                application_version=full.application_version,
                dataset_name=full.dataset_name,
                dataset_version=full.dataset_version,
                evaluator_name=full.evaluator_name,
                evaluator_version=full.evaluator_version,
                provider_type=full.provider_type,
                status=full.status,
                created_at=full.created_at,
                completed_at=full.completed_at,
                case_count=full.case_count,
                mean_score=full.mean_score,
                pass_rate=full.pass_rate,
                avg_latency_ms=full.avg_latency_ms,
            )
        )
    return summaries
