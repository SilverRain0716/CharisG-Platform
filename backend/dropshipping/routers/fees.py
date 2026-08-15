"""DS Fees — 마켓별 Amazon Referral Fee 매핑 조회."""
from fastapi import APIRouter, Depends, Query

from backend.dropshipping.auth import current_user
from backend.dropshipping.services.amazon_fee_service import (
    get_amazon_category,
    calc_real_margin,
)

router = APIRouter(prefix="/api/ds/fees", tags=["ds-fees"])


@router.get("/categories")
def list_categories(market: str = Query(default="US"), user: dict = Depends(current_user)):
    from backend.dropshipping.services.marketplace_config import get_config
    cfg = get_config(market)
    fee_table = cfg["fee_table"]
    return [
        {
            "category": k,
            "fee_pct": round(v.get("rate", 0) * 100, 2),
            "tier": v.get("tier"),
            "market": market,
        }
        for k, v in fee_table.items()
    ]


@router.get("/category")
def fee_for_category(category: str = "", product_name: str = "",
                     market: str = Query(default="US"),
                     user: dict = Depends(current_user)):
    cat = get_amazon_category(category, product_name)
    return {"input": {"category": category, "product_name": product_name},
            "amazon_category": cat, "market": market}


@router.get("/calc")
def calc_margin(
    cost: float, sale: float, shipping: float = 0,
    category: str = "Everything Else",
    market: str = Query(default="US"),
    user: dict = Depends(current_user),
):
    margin = calc_real_margin(
        source_price=cost, ship_cost=shipping, sale_price=sale,
        category=category, product_name="",
    )
    return {"cost": cost, "sale": sale, "shipping": shipping,
            "category": category, "market": market, "real_margin_pct": margin}
