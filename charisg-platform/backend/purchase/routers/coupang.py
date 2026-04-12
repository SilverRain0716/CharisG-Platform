"""PA Coupang — 쿠팡 리스팅 조회 + WING 업로드."""
import json

from fastapi import APIRouter, Depends, HTTPException

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services.image_downloader import mark_images_for_deletion
from backend.purchase.services.coupang_service import register_product, get_orders

router = APIRouter(prefix="/api/pa/coupang", tags=["pa-coupang"])


@router.get("/listings")
def list_listings(user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT l.*, p.title_ko, p.title_en, p.asin
               FROM listings_pa l JOIN products p ON l.product_id = p.id
               WHERE l.channel = 'coupang'
               ORDER BY l.id DESC""",
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/upload/{product_id}")
def upload(product_id: int, user: dict = Depends(current_user)):
    with get_db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        raise HTTPException(404, "상품 없음")

    payload = {
        "displayCategoryCode": p["category_path"] or "",
        "sellerProductName": p["title_ko"] or p["title_en"],
        "salePrice": int(p["sale_price_krw"] or 0),
        "originalPrice": int(p["sale_price_krw"] or 0),
        "items": [{"sellerProductItemName": "기본"}],
    }
    result = register_product(payload)
    if not result:
        return {"ok": False, "error": "쿠팡 API 호출 실패"}

    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO listings_pa
               (product_id, channel, channel_product_id, status, last_synced_at)
               VALUES (?, 'coupang', ?, 'listed', CURRENT_TIMESTAMP)""",
            (product_id, str(result.get("data", "") if isinstance(result, dict) else "")),
        )
    mark_images_for_deletion(product_id)
    return {"ok": True, "result": result}


@router.get("/orders")
def fetch_orders(start: str, end: str, user: dict = Depends(current_user)):
    return {"orders": get_orders(start, end) or []}
