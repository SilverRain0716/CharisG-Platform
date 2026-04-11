"""PA Products — 활성 상품 목록 + 상세 + 상태 변경."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.database import get_db

router = APIRouter(prefix="/api/pa/products", tags=["pa-products"])


@router.get("")
def list_products(
    user: dict = Depends(current_user),
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    where = ["business_model='purchase'"]
    params: list = []
    if status:
        where.append("status=?")
        params.append(status)
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT id, asin, title_ko, title_en, sale_price_krw, cost_usd, margin_pct,
                       category_path, status, bsr, created_at
                FROM products WHERE {' AND '.join(where)}
                ORDER BY id DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) c FROM products WHERE {' AND '.join(where)}", tuple(params),
        ).fetchone()["c"]
    return {"items": [dict(r) for r in rows], "total": total}


@router.get("/{pid}")
def get_product(pid: int, user: dict = Depends(current_user)):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "상품 없음")
        listings = conn.execute(
            "SELECT * FROM listings_pa WHERE product_id=?", (pid,),
        ).fetchall()
        margin = conn.execute(
            """SELECT * FROM margin_calcs WHERE sourcing_id=
               (SELECT sourcing_id FROM products WHERE id=?) ORDER BY id DESC LIMIT 1""",
            (pid,),
        ).fetchone()
    return {
        "product": dict(row),
        "listings": [dict(l) for l in listings],
        "margin": dict(margin) if margin else None,
    }


class StatusBody(BaseModel):
    status: str


@router.patch("/{pid}/status")
def set_status(pid: int, body: StatusBody, user: dict = Depends(current_user)):
    if body.status not in {"draft", "ready", "listed", "active", "paused", "removed"}:
        raise HTTPException(400, "invalid status")
    with get_db() as conn:
        conn.execute(
            "UPDATE products SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (body.status, pid),
        )
    return {"ok": True}
