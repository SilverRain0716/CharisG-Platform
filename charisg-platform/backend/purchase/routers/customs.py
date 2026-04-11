"""PA Customs — 통관 리스크 + AI HS 분류."""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.services import customs_service, tariff_service

router = APIRouter(prefix="/api/pa/customs", tags=["pa-customs"])


class QuickCheck(BaseModel):
    amazon_price_usd: float
    title: str = ""
    sourcing_id: Optional[int] = None


@router.post("/quick")
def quick_check(req: QuickCheck, user: dict = Depends(current_user)):
    result = customs_service.quick_check(req.amazon_price_usd, req.title)
    if req.sourcing_id:
        customs_service.save_check(req.sourcing_id, result)
    return result


class TariffRequest(BaseModel):
    title: str
    description: str = ""


@router.post("/tariff")
async def tariff(req: TariffRequest, user: dict = Depends(current_user)):
    return await tariff_service.get_tariff_info(req.title, req.description)


@router.get("/hs/{hs_code}")
def lookup(hs_code: str, user: dict = Depends(current_user)):
    return customs_service.lookup_hs_code(hs_code) or {"found": False}
