"""PA 가격 산정 라우터 — 채널별 목표 마진 역산."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.purchase.auth import current_user
from backend.purchase.services import pricing_service_pa as svc

router = APIRouter(prefix="/api/pa/pricing", tags=["pa-pricing"])


class CalcBody(BaseModel):
    cost_usd: float = Field(..., ge=0)
    amazon_shipping_usd: float = Field(0.0, ge=0)
    cj_shipping_usd: float = Field(0.0, ge=0)
    channel: str = "smartstore"
    target_margin_override: float | None = None


@router.post("/calculate")
def calculate(body: CalcBody, user: dict = Depends(current_user)):
    try:
        return svc.calculate_sale_krw(
            cost_usd=body.cost_usd,
            amazon_shipping_usd=body.amazon_shipping_usd,
            cj_shipping_usd=body.cj_shipping_usd,
            channel=body.channel,
            target_margin_override=body.target_margin_override,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
