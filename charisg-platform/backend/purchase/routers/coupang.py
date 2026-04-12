"""PA Coupang — 쿠팡 리스팅 조회 + WING 업로드."""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services.image_downloader import mark_images_for_deletion
from backend.purchase.services.coupang_service import register_product, get_orders

logger = logging.getLogger(__name__)
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


@router.post("/upload-all")
def upload_all(user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT l.product_id FROM listings_pa l
               JOIN products p ON l.product_id = p.id
               WHERE l.channel='coupang' AND l.status='pending'
               ORDER BY l.product_id"""
        ).fetchall()
    if not rows:
        raise HTTPException(400, "업로드 대상 없음 (pending 상태 리스팅 필요)")

    results = []
    errors = []
    for r in rows:
        pid = r["product_id"]
        try:
            with get_db() as conn:
                p = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            if not p:
                raise ValueError(f"상품 {pid} 없음")

            payload = {
                "displayCategoryCode": p["category_path"] or "",
                "sellerProductName": p["title_ko"] or p["title_en"],
                "salePrice": int(p["sale_price_krw"] or 0),
                "originalPrice": int(p["sale_price_krw"] or 0),
                "items": [{"sellerProductItemName": "기본"}],
            }
            result = register_product(payload)
            if not result:
                raise ValueError("쿠팡 API 호출 실패")

            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET channel_product_id=?, status='listed',
                       last_synced_at=CURRENT_TIMESTAMP WHERE product_id=? AND channel='coupang'""",
                    (str(result.get("data", "") if isinstance(result, dict) else ""), pid),
                )
            mark_images_for_deletion(pid)
            results.append({"product_id": pid, "ok": True})
        except Exception as e:
            logger.warning(f"[coupang-upload-all] product {pid} 실패: {e}")
            errors.append({"product_id": pid, "error": str(e)})

    return {"uploaded": len(results), "errors": len(errors), "error_details": errors}


@router.get("/orders")
def fetch_orders(start: str, end: str, user: dict = Depends(current_user)):
    return {"orders": get_orders(start, end) or []}
