from __future__ import annotations

from agentforge_core.schemas import EvaluatorOut
from agentforge_evaluators import list_evaluators
from fastapi import APIRouter

router = APIRouter(prefix="/evaluators", tags=["evaluators"])


@router.get("", response_model=list[EvaluatorOut])
async def get_evaluators() -> list[EvaluatorOut]:
    """Every registered evaluator (all deterministic -- none is an LLM judge)."""
    return [
        EvaluatorOut(name=s.name, version=s.version, key=s.key, kind=s.kind, description=s.description)
        for s in list_evaluators()
    ]
