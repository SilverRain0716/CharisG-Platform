"""
coupang_lister.py — 쿠팡 마켓플레이스 리스팅 모듈.

products → 쿠팡 WING 페이로드 변환 → 등록.
스마트스토어 lister 패턴을 따른다.

⚠️ build_payload는 Phase 0-3 (운영자 수동 등록 페이로드 캡처) 후 보정이 필요.
현재는 공식 문서 + Naver 페이로드 매핑 기반 임시 템플릿.
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from backend.purchase.database import get_db
from backend.purchase.services.coupang_service import register_product
from backend.purchase.services import policy_constants as P
from backend.purchase.services.coupang_meta import get_category_meta, build_default_notices
from backend_shared._config import (
    COUPANG_VENDOR_ID,
    COUPANG_USER_ID,
    COUPANG_OUTBOUND_SHIPPING_PLACE_CODE,
    COUPANG_RETURN_CENTER_CODE,
    PUBLIC_BASE_URL,
)

logger = logging.getLogger(__name__)


# ── 상품명 정리 (스마트스토어 lister와 동일 규칙) ────────────
_SPECIAL_CHAR_MAP = {
    '"': '인치', '\u201c': '인치', '\u201d': '인치',
    '*': 'x', '\\': ' ', '?': ' ', '<': '(', '>': ')',
}
_SPECIAL_RE = re.compile('[' + re.escape(''.join(_SPECIAL_CHAR_MAP.keys())) + ']')


def _clean_product_name(name: str) -> str:
    def _replace(m):
        return _SPECIAL_CHAR_MAP.get(m.group(0), ' ')
    cleaned = _SPECIAL_RE.sub(_replace, name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:P.MAX_PRODUCT_NAME_LEN]


def _extract_brand(name: str) -> str:
    words = name.split()
    if words and re.match(r'^[A-Za-z]', words[0]) and len(words[0]) >= 2:
        brand = words[0]
        if len(words) > 1 and re.match(r'^[A-Za-z]', words[1]) and len(words[1]) >= 2:
            brand = f"{words[0]} {words[1]}"
        return brand[:30]
    return "해외 브랜드"


# ── 상세페이지 정적 배너 (Charis G 브랜드/배송/반품 안내) ──
# scripts/render_coupang_banners.py 로 한 번 렌더한 정적 JPG를 전 상품 공통 첨부.
# 내용 수정이 필요하면 templates/coupang_banners_src/*.html 수정 → 스크립트 재실행.
STATIC_BANNER_PATHS = (
    "/api/pa/images/banners/banner_1_brand.jpg",
    "/api/pa/images/banners/banner_2_shipping.jpg",
    "/api/pa/images/banners/banner_3_policy.jpg",
)


# ── 금지 카테고리 (해외구매대행 등록 불가) ──────────────────
# Phase 0-4 검색 결과 기반. 코드 상수로 시작 → 추후 DB 테이블 검토.
PROHIBITED_CATEGORY_KEYWORDS = (
    "의약품", "의료기기", "콘택트렌즈", "안경",
    "주류", "담배", "전자담배",
    "건강기능식품",  # 일반은 가능하나 기능성 광고 시 허가 필요
    "농수산물", "신선식품", "축산물", "수산물",
    "기능성화장품",
    "농약", "총포", "도검", "음란", "유해화학",
)


def _is_prohibited_category(category_name: str) -> tuple[bool, str]:
    """카테고리명에 금지 키워드가 포함되면 True."""
    if not category_name:
        return False, ""
    for kw in PROHIBITED_CATEGORY_KEYWORDS:
        if kw in category_name:
            return True, kw
    return False, ""


def _validate_payload(name: str, price: int, category: str, image_count: int) -> tuple[bool, str]:
    if not name or len(name) < 2:
        return False, "상품명이 너무 짧습니다 (최소 2자)"
    if len(name) > P.MAX_PRODUCT_NAME_LEN:
        return False, f"상품명이 {P.MAX_PRODUCT_NAME_LEN}자 초과 ({len(name)}자)"
    if price < 1000:
        return False, f"판매가 1000원 미만 ({price}원)"
    if not category or not str(category).isdigit():
        return False, f"카테고리 ID가 숫자 형식 아님 ({category})"
    if image_count < 1:
        return False, "이미지 없음"
    return True, ""


def _get_product_images(product_id: int) -> list[str]:
    """상품 이미지 URL 목록 — public_url을 PUBLIC_BASE_URL 절대 경로로 변환해 반환.

    쿠팡은 외부 https URL을 그대로 pull하므로 CharisG가 서빙하는 이미지 경로를 쓴다.
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT public_url FROM image_cache
               WHERE product_id=? AND public_url IS NOT NULL
               ORDER BY image_idx ASC""",
            (product_id,),
        ).fetchall()
    base = PUBLIC_BASE_URL.rstrip("/")
    urls = []
    for r in rows:
        pu = r["public_url"]
        if not pu:
            continue
        urls.append(pu if pu.startswith("http") else f"{base}{pu}")
    return urls


# ── 페이로드 빌드 ──────────────────────────────────────────────

def build_payload(product_id: int, image_urls: list[str] | None = None) -> Optional[dict]:
    """쿠팡 sellerProducts POST 페이로드 빌드.

    ⚠️ Phase 0-3 (운영자 수동 등록 페이로드 캡처) 후 보정 필요.
    """
    with get_db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not p:
            return None
        listing = conn.execute(
            "SELECT sale_krw, coupang_category_code FROM listings_pa WHERE product_id=? AND channel='coupang'",
            (product_id,),
        ).fetchone()

    raw_name = (p["title_ko"] or p["title_en"] or "").strip()
    name = _clean_product_name(raw_name)
    brand = _extract_brand(raw_name)
    price = int(listing["sale_krw"]) if listing and listing["sale_krw"] else int(p["sale_price_krw"] or 0)
    category = str(listing["coupang_category_code"]) if listing and listing["coupang_category_code"] else ""

    if image_urls is None:
        image_urls = _get_product_images(product_id)

    ok, err = _validate_payload(name, price, category, len(image_urls))
    if not ok:
        logger.warning(f"[coupang] product {product_id} 검증 실패: {err}")
        return None

    # 카테고리 메타 prefetch (notices 자동 채움)
    meta = get_category_meta(category)
    notices = build_default_notices(meta) if meta else []

    # 판매 시작/종료
    now = datetime.now(timezone.utc)
    sale_started_at = now.strftime("%Y-%m-%dT%H:%M:%S")
    sale_ended_at = (now + timedelta(days=365 * 5)).strftime("%Y-%m-%dT%H:%M:%S")

    # 이미지 — 첫 1개 REPRESENTATION, 나머지 ≤8 DETAIL.
    # 실제 캡처는 vendorPath 키 사용 (vendorImagePath 아님).
    images_payload = []
    for i, url in enumerate(image_urls[:9]):
        images_payload.append({
            "imageOrder": i,
            "imageType": "REPRESENTATION" if i == 0 else "DETAIL",
            "vendorPath": url,
        })

    # 상세 contents 구성: 상품 이미지(동적) + 정적 정보 배너(전 상품 공통).
    # 쿠팡은 contentsType=HTML에서 inline style 대부분 strip → 이미지 방식으로 통일.
    # 배너 수정은 templates/coupang_banners_src/*.html 편집 후 render_coupang_banners.py 재실행.
    base = PUBLIC_BASE_URL.rstrip("/")
    contents_payload = []
    for url in image_urls[:10]:
        contents_payload.append({
            "contentsType": "IMAGE_NO_SPACE",
            "contentDetails": [{"content": url, "detailType": "IMAGE", "altText": ""}],
        })
    for rel in STATIC_BANNER_PATHS:
        contents_payload.append({
            "contentsType": "IMAGE_NO_SPACE",
            "contentDetails": [{"content": f"{base}{rel}", "detailType": "IMAGE", "altText": ""}],
        })

    payload = {
        "displayCategoryCode": int(category),
        "sellerProductName": name,
        "vendorId": COUPANG_VENDOR_ID,
        "saleStartedAt": sale_started_at,
        "saleEndedAt": sale_ended_at,
        "brand": brand,
        "manufacture": brand,
        "deliveryMethod": "AGENT_BUY",                 # 구매대행
        "deliveryCompanyCode": P.DELIVERY_COMPANY_COUPANG,
        "deliveryChargeType": P.DELIVERY_FEE_TYPE,     # FREE
        "deliveryCharge": 0,
        "freeShipOverAmount": 0,
        "deliveryChargeOnReturn": P.COUPANG_RETURN_FEE,
        "remoteAreaDeliverable": "N",
        "unionDeliveryType": "NOT_UNION_DELIVERY",
        "returnCenterCode": COUPANG_RETURN_CENTER_CODE,
        "returnChargeName": P.RETURN_CHARGE_NAME,
        "companyContactNumber": P.RETURN_CONTACT_NUMBER,
        "returnZipCode": P.RETURN_ZIP_CODE,
        "returnAddress": P.RETURN_ADDRESS,
        "returnAddressDetail": P.RETURN_ADDRESS_DETAIL,
        "returnCharge": P.COUPANG_RETURN_FEE,
        "outboundShippingPlaceCode": COUPANG_OUTBOUND_SHIPPING_PLACE_CODE,
        "vendorUserId": COUPANG_USER_ID or COUPANG_VENDOR_ID,  # WING 로그인 계정 ID
        "requested": False,
        "items": [{
            "itemName": name[:50],
            "originalPrice": price,
            "salePrice": price,
            "maximumBuyCount": P.DEFAULT_STOCK,
            "maximumBuyForPerson": 0,
            "outboundShippingTimeDay": 4,           # 실제 캡처 확인값 (구매대행 해외)
            "maximumBuyForPersonPeriod": 1,
            "unitCount": 1,
            "adultOnly": "EVERYONE",
            "taxType": "TAX",
            "parallelImported": "NOT_PARALLEL_IMPORTED",
            "overseasPurchased": "OVERSEAS_PURCHASED",
            "pccNeeded": True,                       # 통관번호 필수
            "externalVendorSku": f"PA-{product_id}",
            "barcode": "",
            "emptyBarcode": True,
            "emptyBarcodeReason": "COUPANG",
            "modelNo": name[:50],
            "extraProperties": {},
            "certifications": [],
            "searchTags": [],
            "images": images_payload,
            "notices": notices,
            "attributes": [],
            "contents": contents_payload,
            "offerCondition": "NEW",
        }],
        "requiredDocuments": [],  # 구매대행은 구비서류 불필요. 빈 경로 전송 시 자동 반려됨.
        "extraInfoMessage": "",
        "manufactureName": brand,
    }
    return payload


def _sync_product_status(conn, product_id: int):
    """리스팅 채널 중 하나라도 listed/active이면 products.status를 listed로 승격."""
    row = conn.execute(
        """SELECT 1 FROM listings_pa
           WHERE product_id=? AND status IN ('listed', 'active') LIMIT 1""",
        (product_id,),
    ).fetchone()
    if row:
        conn.execute("UPDATE products SET status='listed' WHERE id=? AND status!='listed'", (product_id,))


def list_product(product_id: int, image_urls: list[str] | None = None) -> dict:
    """단일 상품 등록.

    응답 분기:
        - 이미 등록됨: {"ok": False, "skip": True, "error": "..."}
        - 카테고리 금지(사전 차단): {"ok": False, "skip": True, "error": "..."}
        - 페이로드 검증 실패: {"ok": False, "error": "..."}
        - API _skip: {"ok": False, "skip": True, "error": ...} + listings_pa.status='excluded'
        - 성공: {"ok": True, "result": ...} + listings_pa.status='listed'
    """
    with get_db() as conn:
        existing = conn.execute(
            """SELECT channel_product_id, coupang_category_code FROM listings_pa
               WHERE product_id=? AND channel='coupang'""",
            (product_id,),
        ).fetchone()
    if existing and existing["channel_product_id"]:
        return {"ok": False, "skip": True,
                "error": f"이미 등록됨 (channel_product_id={existing['channel_product_id']})"}

    # 사전 카테고리 차단 (coupang_categories.path 기준 키워드 검사)
    if existing and existing["coupang_category_code"]:
        with get_db() as conn:
            cat_name_row = conn.execute(
                "SELECT name, path FROM coupang_categories WHERE code=? LIMIT 1",
                (existing["coupang_category_code"],),
            ).fetchone()
        if cat_name_row:
            cat_text = f"{cat_name_row['path'] or ''} {cat_name_row['name'] or ''}"
            blocked, kw = _is_prohibited_category(cat_text)
            if blocked:
                with get_db() as conn:
                    conn.execute(
                        """UPDATE listings_pa SET status='excluded',
                           error_message=?, last_synced_at=CURRENT_TIMESTAMP
                           WHERE product_id=? AND channel='coupang'""",
                        (f"금지 카테고리 사전 차단 ({kw})", product_id),
                    )
                return {"ok": False, "skip": True, "error": f"금지 카테고리 ({kw})"}

    payload = build_payload(product_id, image_urls=image_urls)
    if not payload:
        return {"ok": False, "error": f"product {product_id}: 페이로드 생성 실패"}

    result = register_product(payload)
    if not result:
        return {"ok": False, "error": "쿠팡 API 호출 실패"}

    if result.get("_skip"):
        with get_db() as conn:
            conn.execute(
                """UPDATE listings_pa SET status='excluded', error_message=?,
                   last_synced_at=CURRENT_TIMESTAMP
                   WHERE product_id=? AND channel='coupang'""",
                (result["_skip"], product_id),
            )
        return {"ok": False, "skip": True, "error": result["_skip"]}

    if result.get("_error"):
        with get_db() as conn:
            conn.execute(
                """UPDATE listings_pa SET status='pending', error_message=?,
                   last_synced_at=CURRENT_TIMESTAMP
                   WHERE product_id=? AND channel='coupang'""",
                (result["_error"][:500], product_id),
            )
        return {"ok": False, "error": result["_error"]}

    seller_product_id = str(result.get("data", "") if isinstance(result, dict) else "")
    with get_db() as conn:
        conn.execute(
            """UPDATE listings_pa SET channel_product_id=?, status='listed',
               last_synced_at=CURRENT_TIMESTAMP, error_message=NULL
               WHERE product_id=? AND channel='coupang'""",
            (seller_product_id, product_id),
        )
        _sync_product_status(conn, product_id)
    return {"ok": True, "result": result}
