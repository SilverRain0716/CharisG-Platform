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

EC2_PUBLIC_BASE = "http://52.79.125.138"


def _get_product_images(product_id: int) -> list[str]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT public_url FROM image_cache WHERE product_id=? ORDER BY image_idx",
            (product_id,),
        ).fetchall()
    if not rows:
        return []
    return [f"{EC2_PUBLIC_BASE}{r['public_url']}" for r in rows]


def _validate_payload(name: str, price: int, category: str, detail_html: str) -> tuple[bool, str]:
    if not name or len(name) < 2:
        return False, "상품명이 너무 짧습니다 (최소 2자)"
    if len(name) > 100:
        return False, f"상품명이 100자를 초과합니다 ({len(name)}자)"
    if price < 1000:
        return False, f"판매가가 최소 금액(1,000원) 미만입니다 ({price}원)"
    if not category:
        return False, "카테고리 ID가 없습니다"
    if not detail_html or len(detail_html) < 10:
        return False, "상세페이지 HTML이 없거나 너무 짧습니다"
    return True, ""


def build_payload(product_id: int) -> Optional[dict]:
    with get_db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not p:
            return None
        detail = conn.execute(
            "SELECT html_content FROM detail_pages WHERE product_id=? ORDER BY updated_at DESC LIMIT 1",
            (product_id,),
        ).fetchone()

    name = (p["title_ko"] or p["title_en"] or "").strip()
    price = int(p["sale_price_krw"] or 0)
    category = p["category_path"] or ""
    desc_html = detail["html_content"] if detail and detail["html_content"] else ""

    ok, err = _validate_payload(name, price, category, desc_html)
    if not ok:
        logger.warning(f"[smartstore] product {product_id} 검증 실패: {err}")
        return None

    image_urls = _get_product_images(product_id)
    images_payload = {}
    if image_urls:
        images_payload["representativeImage"] = {"url": image_urls[0]}
        if len(image_urls) > 1:
            images_payload["optionalImages"] = [{"url": u} for u in image_urls[1:9]]
    else:
        logger.warning(f"[smartstore] product {product_id}: 이미지 없음")
        images_payload["representativeImage"] = {"url": ""}

    payload = {
        "originProduct": {
            "name": name[:100],
            "salePrice": price,
            "stockQuantity": 100,
            "categoryId": category,
            "detailContent": desc_html,
            "images": images_payload,
            "customsDutyInfo": {
                "originAreaCode": "0220037",
                "importDeclarationNumber": "",
                "customsClearanceFlag": False,
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
        return {"ok": False, "error": f"product {product_id}: 페이로드 생성 실패 (검증 오류 또는 상품 없음)"}
    result = register_product(payload)
    if not result:
        return {"ok": False, "error": "naver api 호출 실패"}

    with get_db() as conn:
        conn.execute(
            """UPDATE listings_pa SET channel_product_id=?, status='listed',
               last_synced_at=CURRENT_TIMESTAMP
               WHERE product_id=? AND channel='smartstore'""",
            (str(result.get("originProductNo", "")), product_id),
        )
    return {"ok": True, "result": result}
