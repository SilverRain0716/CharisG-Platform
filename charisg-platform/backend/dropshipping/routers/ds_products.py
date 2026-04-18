"""DS Products — 상품 목록 + 칸반 + 상세 + 상태 변경."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.dropshipping.auth import current_user
from backend.dropshipping.database import get_db

router = APIRouter(prefix="/api/ds/products", tags=["ds-products"])


@router.get("")
def list_products(
    user: dict = Depends(current_user),
    status: Optional[str] = None,
    matrix: Optional[str] = None,
    go: Optional[str] = None,
    sort: str = "sort_score",
    direction: str = "desc",
    limit: int = 100,
    offset: int = 0,
):
    where = ["business_model='dropship'", "hard_filter_pass=1"]
    params: list = []
    if status:
        where.append("status=?"); params.append(status)
    if matrix:
        where.append("matrix_group=?"); params.append(matrix)
    if go:
        where.append("go_decision=?"); params.append(go)

    valid_sort = {"sort_score", "demand_score", "margin_score", "real_margin_pct", "calculated_price"}
    if sort not in valid_sort:
        sort = "sort_score"
    direction = "ASC" if direction.lower() == "asc" else "DESC"

    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT id, product_name, category, amazon_category,
                       source_price, calculated_price, real_margin_pct, adjusted_margin_pct,
                       demand_score, demand_grade, gap_score,
                       margin_score, margin_grade, matrix_group, sort_score,
                       go_decision, status, tier, image_url, url, search_keyword,
                       matched_asin
                FROM collected_products
                WHERE {" AND ".join(where)}
                ORDER BY {sort} {direction}
                LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) c FROM collected_products WHERE {' AND '.join(where)}",
            tuple(params),
        ).fetchone()["c"]

    return {"items": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/kanban")
def kanban_data(user: dict = Depends(current_user)):
    """4열 칸반: candidate / listed / active / paused."""
    cols = {"candidate": [], "listed": [], "active": [], "paused": []}
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, product_name, image_url, status, real_margin_pct, matrix_group, tier
               FROM collected_products WHERE business_model='dropship' AND status IN ('candidate','listed','active','paused')
               ORDER BY sort_score DESC LIMIT 200"""
        ).fetchall()
    for r in rows:
        cols.setdefault(r["status"], []).append(dict(r))
    return [
        {"id": k, "label": k.title(), "items": v}
        for k, v in cols.items()
    ]


@router.get("/{product_id}")
def get_product(product_id: int, user: dict = Depends(current_user)):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM collected_products WHERE id=?", (product_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "상품 없음")
        listing = conn.execute(
            "SELECT * FROM listings WHERE product_id=? ORDER BY id DESC LIMIT 1", (product_id,)
        ).fetchone()
    return {"product": dict(row), "listing": dict(listing) if listing else None}


class StatusUpdate(BaseModel):
    status: str
    note: Optional[str] = None


@router.patch("/{product_id}/status")
def update_status(product_id: int, body: StatusUpdate, user: dict = Depends(current_user)):
    if body.status not in {"candidate", "listed", "active", "paused", "removed"}:
        raise HTTPException(400, "invalid status")
    with get_db() as conn:
        conn.execute(
            "UPDATE collected_products SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (body.status, product_id),
        )
    return {"ok": True, "status": body.status}


class BulkStatusUpdate(BaseModel):
    ids: list[int]
    status: str


@router.post("/bulk-status")
def bulk_status(body: BulkStatusUpdate, user: dict = Depends(current_user)):
    if body.status not in {"candidate", "listed", "active", "paused", "removed"}:
        raise HTTPException(400, "invalid status")
    with get_db() as conn:
        for pid in body.ids:
            conn.execute(
                "UPDATE collected_products SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (body.status, pid),
            )
    return {"ok": True, "updated": len(body.ids)}
