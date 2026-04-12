"""PA Smartstore — 네이버 스마트스토어 리스팅 조회 + 업로드."""
import logging

from fastapi import APIRouter, Depends, HTTPException

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services.image_downloader import mark_images_for_deletion
from backend.purchase.services.smartstore_lister import list_product, build_payload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pa/smartstore", tags=["pa-smartstore"])


@router.get("/listings")
def list_listings(user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT l.*, p.title_ko, p.title_en, p.asin
               FROM listings_pa l JOIN products p ON l.product_id = p.id
               WHERE l.channel = 'smartstore'
               ORDER BY l.id DESC""",
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/upload/{product_id}")
def upload(product_id: int, user: dict = Depends(current_user)):
    result = list_product(product_id)
    if result.get("ok"):
        mark_images_for_deletion(product_id)
    return result


@router.post("/upload-all")
def upload_all(user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT l.product_id FROM listings_pa l
               WHERE l.channel='smartstore' AND l.status='pending'
               ORDER BY l.product_id"""
        ).fetchall()
    if not rows:
        raise HTTPException(400, "업로드 대상 없음 (pending 상태 리스팅 필요)")

    results = []
    errors = []
    for r in rows:
        pid = r["product_id"]
        try:
            res = list_product(pid)
            if not res.get("ok"):
                raise ValueError(res.get("error", "업로드 실패"))
            mark_images_for_deletion(pid)
            results.append({"product_id": pid, "ok": True})
        except Exception as e:
            logger.warning(f"[smartstore-upload-all] product {pid} 실패: {e}")
            errors.append({"product_id": pid, "error": str(e)})

    return {"uploaded": len(results), "errors": len(errors), "error_details": errors}


@router.get("/preview/{product_id}")
def preview(product_id: int, user: dict = Depends(current_user)):
    payload = build_payload(product_id)
    if not payload:
        raise HTTPException(404, "상품 없음")
    return payload
