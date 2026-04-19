"""
smartstore_lister.py — 스마트스토어 리스팅 모듈.

products → 네이버 커머스 API 페이로드 변환 → 등록.
4/29 이후: customsDutyInfo 필수 (해외소싱 상품).

등록 전 완성 파이프라인: 상품명+이미지+속성+태그+브랜드를 모두 포함한 페이로드로 1회 등록.
"""
import json
import logging
import re
from typing import Optional

from backend.purchase.database import get_db
from backend.purchase.services.naver_commerce_service import register_product, upload_image, upload_images_batch

logger = logging.getLogger(__name__)

# ── 상품명/브랜드/태그 유틸 (naver_bulk_update.py에서 이식) ────

_SPECIAL_CHAR_MAP = {
    '"': '인치', '\u201c': '인치', '\u201d': '인치',
    '*': 'x', '\\': ' ', '?': ' ', '<': '(', '>': ')',
}
_SPECIAL_RE = re.compile('[' + re.escape(''.join(_SPECIAL_CHAR_MAP.keys())) + ']')


def _clean_product_name(name: str) -> str:
    """네이버 금지 특수문자 치환 + 50자 제한."""
    def _replace(m):
        return _SPECIAL_CHAR_MAP.get(m.group(0), ' ')
    cleaned = _SPECIAL_RE.sub(_replace, name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:50]


def _extract_brand(name: str) -> str:
    """상품명 첫 단어(영문 2자 이상)를 브랜드명 후보로 추출."""
    words = name.split()
    if words and re.match(r'^[A-Za-z]', words[0]) and len(words[0]) >= 2:
        brand = words[0]
        if len(words) > 1 and re.match(r'^[A-Za-z]', words[1]) and len(words[1]) >= 2:
            brand = f"{words[0]} {words[1]}"
        return brand[:30]
    return "해외 브랜드"


def _build_seller_tags(seo_tags_json: str) -> list[dict]:
    """DB의 seo_tags JSON → 네이버 sellerTags 배열."""
    try:
        tags = json.loads(seo_tags_json) if seo_tags_json else []
    except (json.JSONDecodeError, TypeError):
        return []
    if not tags:
        return []
    valid = []
    for t in tags:
        t = t.strip().replace(" ", "")[:20]
        if t and len(t) >= 2:
            valid.append({"text": t})
    return valid[:10]


def _sync_product_status(conn, product_id: int):
    """리스팅 채널 중 하나라도 listed/active이면 products.status를 listed로 승격."""
    row = conn.execute(
        """SELECT COUNT(*) c FROM listings_pa
           WHERE product_id=? AND status IN ('listed','active')""",
        (product_id,),
    ).fetchone()
    if row["c"] > 0:
        conn.execute(
            "UPDATE products SET status='listed' WHERE id=? AND status IN ('draft','ready')",
            (product_id,),
        )


def _upload_one_image_with_retry(local_path: str, retries: int = 3) -> Optional[str]:
    import time as _time
    for attempt in range(retries + 1):
        url = upload_image(local_path)
        if url:
            return url
        if attempt < retries:
            _time.sleep(2.0 * (attempt + 1))
    return None


def _get_product_images(product_id: int) -> list[str]:
    """로컬 이미지를 배치 업로드 (1회 API 호출로 최대 10장). 실패 시 개별 폴백."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT local_path FROM image_cache WHERE product_id=? ORDER BY image_idx",
            (product_id,),
        ).fetchall()
    if not rows:
        return []

    paths = [r["local_path"] for r in rows[:10]]

    # 배치 업로드 시도 (1회 API 호출)
    results = upload_images_batch(paths)
    naver_urls = [url for url in results if url]

    if naver_urls:
        return naver_urls

    # 배치 실패 시 대표이미지만 개별 재시도
    url = _upload_one_image_with_retry(paths[0])
    if url:
        logger.warning(f"[smartstore] product {product_id} 배치 실패 → 대표이미지 개별 업로드 성공")
        return [url]

    logger.error(f"[smartstore] product {product_id} 대표이미지 업로드 실패")
    return []


def preupload_images(product_id: int) -> list[str]:
    """이미지 사전 업로드 (파이프라인 Phase 1용). URL 목록 반환."""
    return _get_product_images(product_id)


def _validate_payload(name: str, price: int, category: str, detail_html: str) -> tuple[bool, str]:
    if not name or len(name) < 2:
        return False, "상품명이 너무 짧습니다 (최소 2자)"
    if len(name) > 50:
        return False, f"상품명이 50자를 초과합니다 ({len(name)}자)"
    if price < 1000:
        return False, f"판매가가 최소 금액(1,000원) 미만입니다 ({price}원)"
    if not category:
        return False, "카테고리 ID가 없습니다"
    if not category.isdigit() or not (6 <= len(category) <= 12):
        return False, f"카테고리 ID가 숫자 형식이 아닙니다 ({category[:30]})"
    if not detail_html or len(detail_html) < 10:
        return False, "상세페이지 HTML이 없거나 너무 짧습니다"
    return True, ""


def build_payload(product_id: int, image_urls: list[str] | None = None) -> Optional[dict]:
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

    raw_name = (p["title_ko"] or p["title_en"] or "").strip()
    name = _clean_product_name(raw_name)
    price = int(listing["sale_krw"]) if listing and listing["sale_krw"] else int(p["sale_price_krw"] or 0)
    category = p["category_path"] or ""
    desc_html = detail["html_content"] if detail and detail["html_content"] else ""

    ok, err = _validate_payload(name, price, category, desc_html)
    if not ok:
        logger.warning(f"[smartstore] product {product_id} 검증 실패: {err}")
        return None

    if image_urls is None:
        image_urls = _get_product_images(product_id)

    if desc_html:
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

    # ── 브랜드/제조사 추출 ──
    brand = _extract_brand(raw_name)
    model_name = name[:50]

    # ── 태그 (sellerTags) ──
    seo_tags = p["seo_tags"] if p["seo_tags"] else "[]"
    seller_tags = _build_seller_tags(seo_tags)

    # ── 속성 (productAttributes) ──
    product_attributes = []
    inferred_json = p["inferred_attributes_json"] if "inferred_attributes_json" in p.keys() else None
    if inferred_json:
        try:
            product_attributes = json.loads(inferred_json)
        except (json.JSONDecodeError, TypeError):
            pass

    # ── detailAttribute 구성 ──
    detail_attribute = {
        "naverShoppingSearchInfo": {
            "modelName": model_name,
            "manufacturerName": brand,
            "brandName": brand,
            "catalogMatchingYn": False,
        },
        "afterServiceInfo": {
            "afterServiceTelephoneNumber": "010-8558-7277",
            "afterServiceGuideContent": "해외 구매대행 상품으로 국내 A/S가 불가합니다. 네이버 톡톡 또는 1:1 문의를 이용해주세요.",
        },
        "originAreaInfo": {
            "originAreaCode": "0204000",
            "content": "미국산(Charis G)",
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
                "itemName": model_name,
                "modelName": model_name,
                "manufacturer": brand,
                "customerServicePhoneNumber": "010-8558-7277",
            },
        },
    }

    if seller_tags:
        detail_attribute["seoInfo"] = {"sellerTags": seller_tags}

    if product_attributes:
        detail_attribute["productAttributes"] = product_attributes

    payload = {
        "originProduct": {
            "statusType": "SALE",
            "name": name,
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
            "detailAttribute": detail_attribute,
        },
        "smartstoreChannelProduct": {
            "channelProductDisplayStatusType": "ON",
            "naverShoppingRegistration": True,
        },
    }
    return payload


def list_product(product_id: int, image_urls: list[str] | None = None) -> dict:
    with get_db() as conn:
        existing = conn.execute(
            """SELECT channel_product_id FROM listings_pa
               WHERE product_id=? AND channel='smartstore'""",
            (product_id,),
        ).fetchone()
    if existing and existing["channel_product_id"]:
        return {"ok": False, "skip": True,
                "error": f"이미 등록됨 (channel_product_id={existing['channel_product_id']})"}

    payload = build_payload(product_id, image_urls=image_urls)
    if not payload:
        return {"ok": False, "error": f"product {product_id}: 페이로드 생성 실패 (검증 오류 또는 상품 없음)"}
    result = register_product(payload)
    if not result:
        return {"ok": False, "error": "naver api 호출 실패"}

    if result.get("_skip"):
        with get_db() as conn:
            conn.execute(
                """UPDATE listings_pa SET status='excluded', error_message=?,
                   last_synced_at=CURRENT_TIMESTAMP
                   WHERE product_id=? AND channel='smartstore'""",
                (result["_skip"], product_id),
            )
        return {"ok": False, "skip": True, "error": result["_skip"]}

    with get_db() as conn:
        conn.execute(
            """UPDATE listings_pa SET channel_product_id=?, status='listed',
               last_synced_at=CURRENT_TIMESTAMP
               WHERE product_id=? AND channel='smartstore'""",
            (str(result.get("originProductNo", "")), product_id),
        )
        # 등록 페이로드에 inferred attributes를 포함했다면 batch-all 중복 처리 방지를 위해 마킹
        if payload.get("originProduct", {}).get("detailAttribute", {}).get("productAttributes"):
            conn.execute(
                "UPDATE products SET attributes_updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (product_id,),
            )
        _sync_product_status(conn, product_id)
    return {"ok": True, "result": result}
