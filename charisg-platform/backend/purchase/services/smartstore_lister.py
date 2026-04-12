"""
smartstore_lister.py — 스마트스토어 리스팅 모듈.

products → 네이버 커머스 API 페이로드 변환 → 등록.
4/29 이후: customsDutyInfo 필수 (해외소싱 상품).
"""
import json
import logging
from typing import Optional

from backend.purchase.database import get_db
from backend.purchase.services.naver_commerce_service import register_product, upload_image

logger = logging.getLogger(__name__)


def _get_product_images(product_id: int) -> list[str]:
    """로컬 이미지를 네이버에 업로드 후 네이버 URL 반환."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT local_path FROM image_cache WHERE product_id=? ORDER BY image_idx",
            (product_id,),
        ).fetchall()
    if not rows:
        return []

    import time as _time
    naver_urls = []
    for idx, r in enumerate(rows[:10]):
        local_path = r["local_path"]
        url = None
        for attempt in range(3):
            url = upload_image(local_path)
            if url:
                break
            _time.sleep(2.0)
        if url:
            naver_urls.append(url)
        else:
            if idx == 0:
                logger.error(f"[smartstore] product {product_id} 대표이미지 업로드 실패")
                return []
            logger.warning(f"[smartstore] product {product_id} 이미지 {idx} 업로드 실패 (스킵)")
        _time.sleep(1.0)
    return naver_urls


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
        listing = conn.execute(
            "SELECT sale_krw FROM listings_pa WHERE product_id=? AND channel='smartstore'",
            (product_id,),
        ).fetchone()
        detail = conn.execute(
            "SELECT html_content FROM detail_pages WHERE product_id=? ORDER BY updated_at DESC LIMIT 1",
            (product_id,),
        ).fetchone()

    name = (p["title_ko"] or p["title_en"] or "").strip()
    price = int(listing["sale_krw"]) if listing and listing["sale_krw"] else int(p["sale_price_krw"] or 0)
    category = p["category_path"] or ""
    desc_html = detail["html_content"] if detail and detail["html_content"] else ""

    ok, err = _validate_payload(name, price, category, desc_html)
    if not ok:
        logger.warning(f"[smartstore] product {product_id} 검증 실패: {err}")
        return None

    image_urls = _get_product_images(product_id)

    if desc_html:
        import re
        local_pattern = re.compile(r'(?:http://[^"]*)?/api/pa/images/products/\d+/img_\d+\.jpg')
        local_matches = local_pattern.findall(desc_html)
        for i, local_url in enumerate(local_matches):
            if i < len(image_urls):
                desc_html = desc_html.replace(local_url, image_urls[i])
            else:
                desc_html = desc_html.replace(local_url, image_urls[0] if image_urls else "")
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
            "statusType": "SALE",
            "name": name[:100],
            "salePrice": price,
            "stockQuantity": 100,
            "leafCategoryId": category,
            "detailContent": desc_html,
            "images": images_payload,
            "deliveryInfo": {
                "deliveryType": "DELIVERY",
                "deliveryAttributeType": "NORMAL",
                "deliveryCompany": "CJGLS",
                "deliveryBundleGroupUsable": True,
                "deliveryBundleGroupId": 57248768,
                "deliveryFee": {
                    "deliveryFeeType": "FREE",
                },
                "claimDeliveryInfo": {
                    "returnDeliveryCompanyPriorityType": "PRIMARY",
                    "returnDeliveryFee": 5000,
                    "exchangeDeliveryFee": 5000,
                    "shippingAddressId": 200297709,
                    "returnAddressId": 200335116,
                    "freeReturnInsuranceYn": False,
                },
            },
            "detailAttribute": {
                "naverShoppingSearchInfo": {
                    "modelName": name[:50],
                    "manufacturerName": "해외 제조사",
                    "brandName": "해외 브랜드",
                    "catalogMatchingYn": False,
                },
                "afterServiceInfo": {
                    "afterServiceTelephoneNumber": "010-8558-7277",
                    "afterServiceGuideContent": "해외 구매대행 상품으로 국내 A/S가 불가합니다. 네이버 톡톡 또는 1:1 문의를 이용해주세요.",
                },
                "originAreaInfo": {
                    "originAreaCode": "0204000",
                    "content": "상세설명 참조",
                    "importer": "Charis G",
                },
                "taxType": "TAX",
                "minorPurchasable": True,
                "customsTaxType": "EXCLUDED",
                "productInfoProvidedNotice": {
                    "productInfoProvidedNoticeType": "ETC",
                    "etc": {
                        "returnCostReason": "네이버 톡톡 또는 1:1 문의",
                        "noRefundReason": "네이버 톡톡 또는 1:1 문의",
                        "qualityAssuranceStandard": "제조사/수입사 품질보증 기준에 따름",
                        "compensationProcedure": "전자상거래 등에서의 소비자보호에 관한 법률에 따름",
                        "troubleShootingContents": "네이버 톡톡 또는 1:1 문의",
                        "itemName": name[:50],
                        "modelName": name[:50],
                        "manufacturer": "상세설명 참조",
                        "customerServicePhoneNumber": "010-8558-7277",
                    },
                },
            },
        },
        "smartstoreChannelProduct": {
            "channelProductDisplayStatusType": "ON",
            "naverShoppingRegistration": True,
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
