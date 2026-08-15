"""PA Returns — 반품/환불."""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.database import get_db_hot as get_db

router = APIRouter(prefix="/api/pa/returns", tags=["pa-returns"])


@router.get("")
def list_returns(
    user: dict = Depends(current_user),
    status: Optional[str] = None,
    channel: Optional[str] = None,
):
    where = []
    params = []
    if status:
        where.append("r.status=?")
        params.append(status)
    if channel:
        where.append("o.channel=?")
        params.append(channel)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT r.*, o.channel AS channel
                FROM returns_pa r LEFT JOIN orders o ON o.id = r.order_id
                {where_sql} ORDER BY r.requested_at DESC LIMIT 200""",
            tuple(params),
        ).fetchall()
    return [dict(r) for r in rows]


class CreateReturn(BaseModel):
    order_id: int
    reason: str


@router.post("")
def create_return(body: CreateReturn, user: dict = Depends(current_user)):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO returns_pa (order_id, reason) VALUES (?, ?)",
            (body.order_id, body.reason),
        )
    return {"ok": True, "id": cur.lastrowid}


class RefundBody(BaseModel):
    refund_krw: float


@router.patch("/{rid}/refund")
def refund(rid: int, body: RefundBody, user: dict = Depends(current_user)):
    with get_db() as conn:
        conn.execute(
            """UPDATE returns_pa SET refund_krw=?, status='refunded',
                       refunded_at=CURRENT_TIMESTAMP WHERE id=?""",
            (body.refund_krw, rid),
        )
    return {"ok": True}
