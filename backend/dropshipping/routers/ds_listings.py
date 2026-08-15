"""DS Listings — 마켓별 리스팅 목록 + 콘텐츠 편집."""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.dropshipping.auth import current_user
from backend.dropshipping.database import get_db

router = APIRouter(prefix="/api/ds/listings", tags=["ds-listings"])


@router.get("")
def list_listings(market: str = Query(default="US"), user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT l.id, l.product_id, l.asin, l.sku, l.tier, l.status, l.title,
                      l.current_price, l.current_stock, l.last_price,
                      l.listed_at, l.activated_at, l.last_synced_at, l.marketplace,
                      p.product_name, p.image_url, p.real_margin_pct, p.source_price
               FROM listings l
               LEFT JOIN collected_products p ON l.product_id = p.id
               WHERE l.marketplace = ?
               ORDER BY l.id DESC""",
            (market,),
        ).fetchall()
        total = len(rows)
        active = sum(1 for r in rows if r["status"] == "active")
        paused = sum(1 for r in rows if r["status"] == "paused")
        listed = sum(1 for r in rows if r["status"] == "listed")

    return {
        "market": market,
        "items": [dict(r) for r in rows],
        "kpis": {
            "total": total,
            "active": active,
            "paused": paused,
            "listed": listed,
        },
    }


class ListingContent(BaseModel):
    title: str
    bullets: list[str] = []
    description: str = ""
    keywords: list[str] = []
    tier: Optional[str] = None


@router.put("/{product_id}/content")
def update_content(product_id: int, body: ListingContent, market: str = Query(default="US"),
                   user: dict = Depends(current_user)):
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM listings WHERE product_id=? AND marketplace=? ORDER BY id DESC LIMIT 1",
            (product_id, market),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE listings SET title=?, bullets=?, description=?, keywords=?,
                                       tier=COALESCE(?, tier), updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (body.title, json.dumps(body.bullets, ensure_ascii=False),
                 body.description, json.dumps(body.keywords, ensure_ascii=False),
                 body.tier, existing["id"]),
            )
            return {"ok": True, "id": existing["id"]}
        else:
            cur = conn.execute(
                """INSERT INTO listings (product_id, business_model, marketplace, tier, status, title, bullets, description, keywords)
                   VALUES (?, 'dropship', ?, ?, 'candidate', ?, ?, ?, ?)""",
                (product_id, market, body.tier or "tier2", body.title,
                 json.dumps(body.bullets, ensure_ascii=False), body.description,
                 json.dumps(body.keywords, ensure_ascii=False)),
            )
            return {"ok": True, "id": cur.lastrowid}


@router.patch("/{listing_id}/move/{new_status}")
def move_listing(listing_id: int, new_status: str, user: dict = Depends(current_user)):
    if new_status not in {"candidate", "listed", "active", "paused"}:
        raise HTTPException(400, "invalid status")
    with get_db() as conn:
        conn.execute(
            "UPDATE listings SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_status, listing_id),
        )
    return {"ok": True, "id": listing_id, "status": new_status}
