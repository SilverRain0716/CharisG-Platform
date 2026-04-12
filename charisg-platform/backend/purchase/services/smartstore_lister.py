"""
smartstore_lister.py — 스마트스토어 리스팅 모듈.

products → 네이버 커머스 API 페이로드 변환 → 등록.
4/29 이후: customsDutyInfo 필수 (해외소싱 상품).
"""
import json
import logging
from typing import Optional

from backend.purchase.database import get_db
from backend.purchase.services.naver_commerce_service import register_product

logger = logging.getLogger(__name__)


def build_payload(product_id: int) -> Optional[dict]:
    with get_db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not p:
            return None
        detail = conn.execute(
            "SELECT html_content FROM detail_pages WHERE product_id=? ORDER BY updated_at DESC LIMIT 1",
            (product_id,),
        ).fetchone()

    desc_html = detail["html_content"] if detail and detail["html_content"] else ""

    payload = {
        "originProduct": {
            "name": p["title_ko"] or p["title_en"],
            "salePrice": int(p["sale_price_krw"] or 0),
            "stockQuantity": 100,
            "categoryId": p["category_path"] or "",
            "detailContent": desc_html,
            "images": {"representativeImage": {"url": ""}},
            # 4/29 이후 필수 — 해외소싱 상품 customsDutyInfo
            "customsDutyInfo": {
                "originAreaCode": "0220037",   # 미국 (예시 코드)
                "importDeclarationNumber": "",
                "customsClearanceFlag": True,
            },
        },
        "smartstoreChannelProduct": {
            "channelProductDisplayStatusType": "ON",
        },
    }
    return payload


def list_product(product_id: int) -> dict:
    payload = build_payload(product_id)
    if not payload:
        return {"ok": False, "error": "product not found"}
    result = register_product(payload)
    if not result:
        return {"ok": False, "error": "naver api 호출 실패"}

    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO listings_pa
               (product_id, channel, channel_product_id, status, last_synced_at)
               VALUES (?, 'smartstore', ?, 'listed', CURRENT_TIMESTAMP)""",
            (product_id, str(result.get("originProductNo", ""))),
        )
    return {"ok": True, "result": result}
