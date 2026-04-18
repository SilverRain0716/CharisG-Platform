"""PA Products — 활성 상품 목록 + 상세 + 상태 변경."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services.channel_listing_service import send_to_channels

router = APIRouter(prefix="/api/pa/products", tags=["pa-products"])


@router.get("")
def list_products(
    user: dict = Depends(current_user),
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    unchanneled_only: bool = False,
):
    """상품 목록. unchanneled_only=True 면 listings_pa 행이 하나도 없는 상품만 (= 아직 채널로 보낸 적 없음)."""
    where = ["p.business_model='purchase'"]
    params: list = []
    if status:
        where.append("p.status=?")
        params.append(status)
    if unchanneled_only:
        where.append("NOT EXISTS (SELECT 1 FROM listings_pa l WHERE l.product_id = p.id)")
    where_sql = " AND ".join(where)
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT p.id, p.asin, p.title_ko, p.title_en, p.sale_price_krw, p.cost_usd, p.margin_pct,
                       p.category_path, p.status, p.bsr, p.ai_processed_at, p.seo_title, p.created_at
                FROM products p WHERE {where_sql}
                ORDER BY p.id DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) c FROM products p WHERE {where_sql}", tuple(params),
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


class SendToChannelBody(BaseModel):
    channels: list[str] = ["smartstore", "coupang"]


@router.post("/{pid}/send-to-channel")
def send_to_channel(pid: int, body: SendToChannelBody, user: dict = Depends(current_user)):
    try:
        result = send_to_channels(pid, body.channels)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return result


@router.post("/bulk-send-to-channel")
def bulk_send_to_channel(body: SendToChannelBody, user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id FROM products
               WHERE business_model='purchase' AND ai_processed_at IS NOT NULL AND cost_usd IS NOT NULL
               ORDER BY id"""
        ).fetchall()
    if not rows:
        raise HTTPException(400, "채널 전송 대상 없음 (AI 처리 완료 + cost_usd 필요)")

    results = []
    errors = []
    for r in rows:
        try:
            res = send_to_channels(r["id"], body.channels)
            results.append(res)
        except Exception as e:
            errors.append({"product_id": r["id"], "error": str(e)})

    return {"sent": len(results), "errors": len(errors), "error_details": errors}


class PriceBody(BaseModel):
    sale_price_krw: int


@router.patch("/{pid}/price")
def set_price(pid: int, body: PriceBody, user: dict = Depends(current_user)):
    if body.sale_price_krw < 0:
        raise HTTPException(400, "판매가는 0 이상이어야 함")
    with get_db() as conn:
        row = conn.execute("SELECT cost_usd FROM products WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "상품 없음")
        cost_usd = row["cost_usd"] or 0
        from backend.purchase.services.exchange_rate_service import get_current_rate
        fx = get_current_rate()
        cost_krw = cost_usd * fx
        margin_pct = ((body.sale_price_krw - cost_krw) / body.sale_price_krw * 100) if body.sale_price_krw > 0 else 0
        conn.execute(
            """UPDATE products SET sale_price_krw=?, margin_pct=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (body.sale_price_krw, round(margin_pct, 1), pid),
        )
        conn.execute(
            """UPDATE listings_pa SET sale_krw=?, net_margin_krw=?, updated_at=CURRENT_TIMESTAMP
               WHERE product_id=?""",
            (body.sale_price_krw, int(body.sale_price_krw * margin_pct / 100), pid),
        )
    return {"ok": True, "sale_price_krw": body.sale_price_krw, "margin_pct": round(margin_pct, 1)}


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


def _cascade_delete_products(conn, product_ids: list[int]) -> dict:
    """products + listings_pa + detail_pages + image_cache cascade 삭제.
    호출자가 connection 관리 (with get_db() as conn).
    """
    if not product_ids:
        return {"products": 0, "listings_pa": 0, "detail_pages": 0, "image_cache": 0}
    placeholders = ",".join("?" * len(product_ids))
    counts = {}
    for table in ("image_cache", "detail_pages", "listings_pa"):
        cur = conn.execute(
            f"DELETE FROM {table} WHERE product_id IN ({placeholders})",
            product_ids,
        )
        counts[table] = cur.rowcount
    cur = conn.execute(
        f"DELETE FROM products WHERE id IN ({placeholders})",
        product_ids,
    )
    counts["products"] = cur.rowcount
    return counts


class BulkDeleteBody(BaseModel):
    ids: list[int] | None = None
    channel: str | None = None
    status: str | None = None  # e.g. 'excluded'


@router.post("/bulk-delete")
def bulk_delete(body: BulkDeleteBody, user: dict = Depends(current_user)):
    """상품 일괄 삭제 (cascade). 두 가지 모드:
    - ids 지정: 정확히 해당 product_id 들 삭제
    - channel + status 지정: 그 채널/상태의 listings_pa가 가리키는 product 전체 삭제
    """
    if body.ids:
        ids = [int(i) for i in body.ids]
    elif body.channel and body.status:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT DISTINCT product_id FROM listings_pa
                   WHERE channel=? AND status=?""",
                (body.channel, body.status),
            ).fetchall()
        ids = [r["product_id"] for r in rows]
    else:
        raise HTTPException(400, "ids 또는 channel+status 필수")

    if not ids:
        return {"deleted": {"products": 0}, "ids": []}

    with get_db() as conn:
        counts = _cascade_delete_products(conn, ids)

    return {"deleted": counts, "ids": ids}
