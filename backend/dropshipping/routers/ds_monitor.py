"""DS Monitor — 마켓별 계정 건강도 + CJ 재고 + Amazon 가격 변동."""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.dropshipping.auth import current_user
from backend.dropshipping.database import get_db

router = APIRouter(prefix="/api/ds/monitor", tags=["ds-monitor"])


@router.get("/health")
def get_health(market: str = Query(default="US"), user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT odr, late_shipment_rate, cancel_rate, valid_tracking_rate,
                      input_type, note, marketplace, updated_at
               FROM account_health WHERE marketplace=? ORDER BY id DESC LIMIT 30""",
            (market,),
        ).fetchall()
        if not rows:
            rows = conn.execute(
                """SELECT odr, late_shipment_rate, cancel_rate, valid_tracking_rate,
                          input_type, note, updated_at
                   FROM account_health ORDER BY id DESC LIMIT 30"""
            ).fetchall()
    if not rows:
        return {"market": market, "current": None, "history": []}
    return {"market": market, "current": dict(rows[0]), "history": [dict(r) for r in rows]}


class HealthInput(BaseModel):
    odr: Optional[float] = None
    late_shipment_rate: Optional[float] = None
    cancel_rate: Optional[float] = None
    valid_tracking_rate: Optional[float] = None
    note: Optional[str] = None


@router.post("/health")
def post_health(body: HealthInput, market: str = Query(default="US"),
                user: dict = Depends(current_user)):
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO account_health
               (odr, late_shipment_rate, cancel_rate, valid_tracking_rate, input_type, note, marketplace)
               VALUES (?, ?, ?, ?, 'manual', ?, ?)""",
            (body.odr, body.late_shipment_rate, body.cancel_rate,
             body.valid_tracking_rate, body.note, market),
        )
    return {"ok": True, "id": cur.lastrowid, "market": market}


@router.get("/stock")
def stock_alerts(market: str = Query(default="US"), user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, product_name, stock_quantity, status
               FROM collected_products
               WHERE business_model='dropship' AND go_decision='GO' AND stock_quantity < 10
               ORDER BY stock_quantity ASC LIMIT 50"""
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/price-changes")
def price_changes(market: str = Query(default="US"), user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT cp.id, cp.product_name, cp.calculated_price, cp.amazon_price_p75,
                      cp.real_margin_pct, cp.adjusted_margin_pct
               FROM collected_products cp
               WHERE cp.business_model='dropship'
                 AND cp.status IN ('listed','active')
                 AND cp.amazon_price_p75 IS NOT NULL"""
        ).fetchall()
    return [dict(r) for r in rows]
