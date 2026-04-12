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
):
    where = ["business_model='purchase'"]
    params: list = []
    if status:
        where.append("status=?")
        params.append(status)
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT id, asin, title_ko, title_en, sale_price_krw, cost_usd, margin_pct,
                       category_path, status, bsr, ai_processed_at, seo_title, created_at
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
