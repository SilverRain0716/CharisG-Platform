"""PA Orders — 6단계 칸반 + 단계 진행."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services.order_receiver_service import advance_step, ORDER_STEPS

router = APIRouter(prefix="/api/pa/orders", tags=["pa-orders"])


@router.get("/kanban")
def kanban(user: dict = Depends(current_user)):
    cols = {step: [] for step, _ in ORDER_STEPS}
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, channel, channel_order_id, customer_name, sale_price_krw,
                      current_step, placed_at FROM orders ORDER BY placed_at DESC LIMIT 200"""
        ).fetchall()
    for r in rows:
        cols.setdefault(r["current_step"], []).append(dict(r))
    return [
        {"id": s, "label": l, "items": cols.get(s, [])}
        for s, l in ORDER_STEPS
    ]


@router.get("")
def list_orders(
    user: dict = Depends(current_user),
    step: Optional[str] = None,
    channel: Optional[str] = None,
    limit: int = 100,
):
    where = []
    params = []
    if step:
        where.append("current_step=?")
        params.append(step)
    if channel:
        where.append("channel=?")
        params.append(channel)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM orders {where_sql} ORDER BY placed_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/{oid}")
def get_order(oid: int, user: dict = Depends(current_user)):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not row:
            raise HTTPException(404, "주문 없음")
        steps = conn.execute(
            "SELECT * FROM order_steps WHERE order_id=? ORDER BY id", (oid,),
        ).fetchall()
    return {"order": dict(row), "steps": [dict(s) for s in steps]}


class AdvanceBody(BaseModel):
    step: str
    note: Optional[str] = None


@router.patch("/{oid}/advance")
def advance(oid: int, body: AdvanceBody, user: dict = Depends(current_user)):
    if not advance_step(oid, body.step, body.note or ""):
        raise HTTPException(400, "invalid step")
    return {"ok": True}
