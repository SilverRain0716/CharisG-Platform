"""PA Margin — 마진 계산 + 저장."""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.services.margin_calculator import (
    MarginInput, calculate, calculate_with_defaults, save_margin,
)
from backend.purchase.database import get_db

router = APIRouter(prefix="/api/pa/margin", tags=["pa-margin"])


class CalcRequest(BaseModel):
    sourcing_id: Optional[int] = None
    amazon_price_usd: float
    sale_price_krw: float
    fx_rate: Optional[float] = None
    customs_duty_krw: float = 0.0
    save: bool = False
    competition: Optional[str] = None


@router.post("/calculate")
def calc(req: CalcRequest, user: dict = Depends(current_user)):
    if req.fx_rate:
        result = calculate(MarginInput(
            amazon_price_usd=req.amazon_price_usd,
            sale_price_krw=req.sale_price_krw,
            fx_rate=req.fx_rate,
            customs_duty_krw=req.customs_duty_krw,
        ))
    else:
        result = calculate_with_defaults(
            req.amazon_price_usd, req.sale_price_krw, req.customs_duty_krw,
        )

    saved_id = None
    if req.save and req.sourcing_id:
        inp = MarginInput(
            amazon_price_usd=req.amazon_price_usd,
            sale_price_krw=req.sale_price_krw,
            fx_rate=req.fx_rate or 1380,
            customs_duty_krw=req.customs_duty_krw,
        )
        saved_id = save_margin(req.sourcing_id, inp, result, req.competition)

    return {**result.__dict__, "saved_id": saved_id}


@router.get("/{sourcing_id}")
def get_for_sourcing(sourcing_id: int, user: dict = Depends(current_user)):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM margin_calcs WHERE sourcing_id=? ORDER BY id DESC LIMIT 1",
            (sourcing_id,),
        ).fetchone()
    return dict(row) if row else None
