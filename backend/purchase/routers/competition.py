"""PA Competition — 경쟁 가격 비교."""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.services.competition_service import (
    grade_competition, store_snapshot, get_recent_snapshots,
)

router = APIRouter(prefix="/api/pa/competition", tags=["pa-competition"])


class GradeRequest(BaseModel):
    my_price: float
    competitor_prices: list[float]


@router.post("/grade")
def grade(req: GradeRequest, user: dict = Depends(current_user)):
    return {"grade": grade_competition(req.my_price, req.competitor_prices)}


@router.get("/{product_id}")
def history(product_id: int, user: dict = Depends(current_user)):
    return get_recent_snapshots(product_id)
