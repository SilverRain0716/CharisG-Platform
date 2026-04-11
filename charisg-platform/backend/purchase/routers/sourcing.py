"""PA Sourcing — 후보 리스트, GO/NO-GO 판단, 일괄 처리."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.database import get_db

router = APIRouter(prefix="/api/pa/sourcing", tags=["pa-sourcing"])


@router.get("")
def list_candidates(
    user: dict = Depends(current_user),
    status: Optional[str] = None,
    shipping: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    where = []
    params = []
    if status:
        where.append("sourcing_status=?")
        params.append(status)
    if shipping:
        where.append("shipping_status=?")
        params.append(shipping)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT id, keyword_id, asin, title, amazon_url, image_url, price_usd,
                       rating, review_count, in_stock, cj_filter_pass,
                       shipping_status, sourcing_status, collected_at
                FROM sourcing_candidates {where_sql}
                ORDER BY collected_at DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) c FROM sourcing_candidates {where_sql}", tuple(params),
        ).fetchone()["c"]
    return {"items": [dict(r) for r in rows], "total": total}


class GoNogoBody(BaseModel):
    decision: str   # 'go' | 'nogo'
    reason: Optional[str] = None


@router.patch("/{sid}/decision")
def make_decision(sid: int, body: GoNogoBody, user: dict = Depends(current_user)):
    if body.decision not in {"go", "nogo"}:
        raise HTTPException(400, "decision must be 'go' or 'nogo'")
    with get_db() as conn:
        conn.execute(
            "UPDATE sourcing_candidates SET sourcing_status=?, nogo_reason=? WHERE id=?",
            (body.decision, body.reason, sid),
        )
    return {"ok": True}


class BulkBody(BaseModel):
    ids: list[int]
    decision: str
    reason: Optional[str] = None


@router.post("/bulk-decision")
def bulk_decision(body: BulkBody, user: dict = Depends(current_user)):
    if body.decision not in {"go", "nogo"}:
        raise HTTPException(400, "decision must be 'go' or 'nogo'")
    with get_db() as conn:
        for sid in body.ids:
            conn.execute(
                "UPDATE sourcing_candidates SET sourcing_status=?, nogo_reason=? WHERE id=?",
                (body.decision, body.reason, sid),
            )
    return {"ok": True, "updated": len(body.ids)}
