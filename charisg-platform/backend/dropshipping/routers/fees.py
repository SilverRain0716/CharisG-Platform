"""DS Fees — Amazon Referral Fee 매핑 조회."""
from fastapi import APIRouter, Depends, HTTPException

from backend.dropshipping.auth import current_user
from backend.dropshipping.services.amazon_fee_service import (
    get_amazon_category,
    calc_real_margin,
)

router = APIRouter(prefix="/api/ds/fees", tags=["ds-fees"])


@router.get("/categories")
def list_categories(user: dict = Depends(current_user)):
    """전체 카테고리 → fee 매핑 (amazon_fee_service 내부 데이터)."""
    try:
        from backend.dropshipping.services.amazon_fee_service import REFERRAL_FEES
        return [{"category": k, "fee_pct": v} for k, v in REFERRAL_FEES.items()]
    except ImportError:
        raise HTTPException(500, "amazon_fee_service.REFERRAL_FEES 누락 — 모노리스에서 import 확인 필요")


@router.get("/category")
def fee_for_category(category: str = "", product_name: str = "", user: dict = Depends(current_user)):
    cat = get_amazon_category(category, product_name)
    return {"input": {"category": category, "product_name": product_name}, "amazon_category": cat}


@router.get("/calc")
def calc_margin(
    cost: float, sale: float, shipping: float = 0, category: str = "Everything Else",
    user: dict = Depends(current_user),
):
    margin = calc_real_margin(cost=cost, sale=sale, shipping=shipping, amazon_category=category)
    return {"cost": cost, "sale": sale, "shipping": shipping, "category": category, "real_margin_pct": margin}
