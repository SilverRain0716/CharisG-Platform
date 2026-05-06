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
from backend.purchase.services import clean_policy
from backend.purchase.services.coupang_service import register_product
from backend.purchase.services import policy_constants as P
from backend.purchase.services.coupang_meta import get_category_meta, build_default_notices
from backend.purchase.services.coupang_attributes import build_required_attributes
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


_BRAND_PLACEHOLDER_RE = re.compile(r'\[\s*브랜드[^\]]*\]\s*')


def _clean_product_name(name: str) -> str:
    # AI 가 출력한 [브랜드명], [브랜드 명], [브랜드명 미포함] 등 placeholder 제거
    name = _BRAND_PLACEHOLDER_RE.sub('', name or '')
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
    "/api/pa/images/banners/banner_3_amazon.jpg",
    "/api/pa/images/banners/banner_4_purchase_notice.jpg",
)


# ── 금지 카테고리 (해외구매대행 등록 불가) ──────────────────
# Phase 0-4 검색 결과 기반. 코드 상수로 시작 → 추후 DB 테이블 검토.
# "건강기능식품"은 영업등록(수입식품등 인터넷구매대행업) 보유 후 제거.
# 대신 _is_banned_ingredient() 와 _strip_efficacy_claims() 로 대체 게이팅.
PROHIBITED_CATEGORY_KEYWORDS = (
    "의약품", "의료기기", "콘택트렌즈", "안경",
    "주류", "담배", "전자담배",
    "농수산물", "신선식품", "축산물", "수산물",
    "기능성화장품",
    "농약", "총포", "도검", "음란", "유해화학",
)


# ── 건강기능식품 카테고리 식별 ────────────────────────────
# 쿠팡 카테고리 path/name 에 아래 키워드가 포함되면 효능 광고 strip 대상.
HEALTH_FOOD_CATEGORY_KEYWORDS = (
    "건강기능식품", "건강식품", "영양제", "보충제", "프로틴",
    "비타민", "오메가", "유산균", "프로바이오틱",
)


# ── 국내 의약품 분류 / 식약처 금지 성분 ───────────────────
# 매칭 시 hard block. 영업등록자라도 판매 불가 (수입금지·의약품 분류).
# Tier 1+2+3 — safety_filter.py 와 동일 list (등록 직전 2차 게이트).
BANNED_INGREDIENT_KEYWORDS = (
    # ── Tier 1: 마약류 / 향정신성 ──
    "Kratom", "크라톰",
    "Ephedra", "에페드라", "ephedrine", "에페드린", "마황",
    "CBD", "Cannabidiol", "칸나비디올",
    "Androstenedione", "안드로스텐디온",
    "Kava Kava", "Kava", "카바", "카바카바",
    "Yohimbe", "Yohimbine", "요힘빈", "요힘베",
    # ── Tier 2: 의약품 원료 ──
    "NAC", "N-Acetyl Cysteine", "N Acetyl Cysteine", "N 아세틸 시스테인", "N-아세틸시스테인",
    "Melatonin", "melatonin", "멜라토닌",
    "DHEA", "디에이치이에이",
    "Pregnenolone", "pregnenolone", "프레그네놀론",
    "5-HTP", "5HTP", "5 HTP", "5-htp",
    "Berberine", "베르베린",
    "Synephrine", "시네프린",
    # ── Tier 3: 식약처 미인정 원료 ──
    "Ashwagandha", "아슈와간다", "아쉬와간다",
    "Maca", "마카",
    "Lion's Mane", "Lion Mane", "Lions Mane", "라이언메인", "사자갈기", "노루궁뎅이버섯",
    "Valerian", "발레리안", "쥐오줌풀",
    "St John", "St. John", "St Johns", "세인트존스워트", "서양고추나물", "성요한초",
    "Mullein", "멀레인",
    "Elderberry", "엘더베리",
    "Astragalus", "황기",
    "Echinacea", "에키네시아",
    "Tongkat Ali", "통캇알리",
    "Turkesterone", "터케스테론", "Ecdysterone", "엑디스테론",
    "Black Seed", "Nigella Sativa", "니젤라",
    "Comfrey", "comfrey", "컴프리",
    # ── 비만 약물 (기존 유지) ──
    "시부트라민", "sibutramine",
    "펜플루라민", "fenfluramine",
    "프로게스테론", "progesterone",
    # ── 기타 국내 미허용 ──
    "콜로이드은", "colloidal silver",
    # ── Tier 4: 한국 수입 완전 금지 (비-성분) ──
    "Marijuana", "Cannabis", "대마", "마리화나",
    "Cocaine", "코카인",
    "Opium", "아편",
    "MDMA", "Amphetamine", "암페타민",
    "firearm", "총기", "모조 총기",
    "sword", "knife", "blade", "도검", "나이프", "칼날",
    "gunpowder", "explosive", "fireworks", "화약", "폭발물", "폭죽",
    "taser", "stun gun", "테이저", "전기충격기",
    "porn", "pornographic", "음란",
    "ivory", "elephant tusk", "상아", "코끼리뼈",
    "tiger", "leopard", "호랑이가죽", "표범가죽",
    "crocodile leather", "alligator leather", "snake leather",
    "악어가죽", "도마뱀가죽",
    "coral jewelry", "산호장식", "shark fin", "상어지느러미", "샥스핀",
    "radioactive", "방사성",
    "dry ice", "드라이아이스",
    "sodastream cylinder", "소다스트림 실린더",
    "live animal", "human remains", "ashes urn", "유골", "인체",
    # ── Tier 5: malltail 통관 거부 사례 ──
    "Sildenafil", "실데나필", "Viagra", "비아그라",
    "HCG", "human chorionic gonadotropin",
    "beef extract", "beef tallow", "우피유래", "우유래",
    "Hoodia", "후디아", "Hoodia Gordonii",
    "Raspberry Ketones", "라즈베리 케톤", "라즈베리케톤",
    "Icariin", "이카린",
    "Horny Goat Weed", "호랑이풀", "호색초",
    "Muira Puama", "무이라푸아마",
    "Catuaba", "카투아바",
    "Tongkat Ali", "통캇알리",
    "Cat's Claw", "Cats Claw", "고양이발톱",
    "Cascara Sagrada", "카스카라",
    "Couch Grass", "카우치그라스",
    "Buchu Leaf", "부추잎",
    "Gymnema Sylvestre", "김네마", "기무네마",
    "Clubmoss", "Club Moss", "클럽모스",
    "Vinpocetine", "빈포세틴",
    "Germanium", "게르마늄",
    "DIM", "Diindolylmethane", "디인돌릴메탄",
    "Cordyceps",
    "L-Citrulline", "시트룰린", "씨트롤린",
)


# ── 기능성 광고 표현 (건강기능식품 카테고리에서만 strip) ──
# 식약처 「건강기능식품 표시·광고 심의기준」 위반 우려 표현.
# 자율심의를 받지 않았다면 이런 효능 표현은 금지.
EFFICACY_CLAIM_PATTERNS = (
    r"면역력\s*(강화|증진|향상|개선)?",
    r"피로\s*(회복|개선|해소)",
    r"항산화",
    r"노화\s*(방지|억제|예방)",
    r"다이어트(\s*효과)?",
    r"체중\s*(감량|조절|관리)",
    r"혈압\s*(개선|조절|강하)",
    r"혈당\s*(개선|조절|관리)",
    r"콜레스테롤\s*(감소|개선|조절)",
    r"기억력\s*(개선|향상|증진)",
    r"집중력\s*(개선|향상|증진)",
    r"관절\s*(건강|개선)",
    r"눈\s*건강",
    r"간\s*건강",
    r"장\s*건강",
    r"전립선\s*건강",
    r"갱년기\s*(개선|증상)",
    r"숙면|수면\s*(개선|유도)",
    r"불면증?\s*(개선|해소)",
    r"질병\s*(예방|치료)",
    r"질환\s*(예방|치료)",
    r"치료\s*효과",
)
_EFFICACY_RE = re.compile("|".join(EFFICACY_CLAIM_PATTERNS), re.IGNORECASE)


def _is_banned_ingredient(title_en: str, title_ko: str) -> Optional[str]:
    """상품명에 국내 금지 성분이 보이면 매칭 키워드 반환. 영업등록과 무관하게 hard block."""
    haystack = f"{title_ko or ''} {title_en or ''}"
    haystack_upper = haystack.upper()
    for kw in BANNED_INGREDIENT_KEYWORDS:
        if not kw:
            continue
        if re.search(r"[A-Za-z]", kw):
            # 영문은 단어 경계 검사 (false positive 방지)
            if re.search(rf"\b{re.escape(kw.upper())}\b", haystack_upper):
                return kw
        else:
            if kw in haystack:
                return kw
    return None


def _is_health_food_category(category_name: str) -> bool:
    if not category_name:
        return False
    return any(kw in category_name for kw in HEALTH_FOOD_CATEGORY_KEYWORDS)


def _strip_efficacy_claims(text: str) -> tuple[str, list[str]]:
    """기능성 광고 표현을 공백으로 치환. 매칭된 원문 리스트도 반환 (로그/감사용).

    심의 미통과 상품에 효능 표현을 그대로 두면 식약처 행정처분 대상.
    영업등록자도 자율심의 별도 → 보수적으로 strip.
    """
    if not text:
        return text, []
    matches = [m.group(0).strip() for m in _EFFICACY_RE.finditer(text)]
    cleaned = _EFFICACY_RE.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, matches


# 쿠팡 유통경로 소명 요청(정품 게이팅)에 걸린 브랜드 기본 차단 목록.
# Why: 거래 내역 없는 구매대행은 소명 불가 → 선제 차단으로 계정 리스크 예방.
# 운영자가 settings 테이블의 'coupang.brand_blocklist' 키로 JSON 배열 저장하면 그 값이 우선.
BRAND_BLOCKLIST_DEFAULT = (
    "NIKE", "ADIDAS", "PUMA", "STANLEY", "LACOSTE", "TITLEIST", "CARHARTT",
    "나이키", "아디다스", "푸마", "스탠리", "라코스테", "타이틀리스트", "칼하트",
)


def _load_brand_blocklist() -> tuple[str, ...]:
    """settings 테이블에서 블랙리스트 로드 (없으면 default)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='coupang.brand_blocklist'"
        ).fetchone()
    if row and row["value"]:
        try:
            items = json.loads(row["value"])
            if isinstance(items, list):
                return tuple(str(x).strip() for x in items if str(x).strip())
        except Exception:
            logger.warning("[coupang] settings.coupang.brand_blocklist JSON 파싱 실패 — default 사용")
    return BRAND_BLOCKLIST_DEFAULT


def _is_brand_blocked(title_en: str, title_ko: str, blocklist: tuple[str, ...]) -> Optional[str]:
    """title에 블랙리스트 브랜드 키워드가 있으면 매칭된 키워드 반환."""
    en = (title_en or "").upper()
    ko = title_ko or ""
    for kw in blocklist:
        if not kw:
            continue
        k_upper = kw.upper()
        # 영문은 단어 경계 검사, 한글은 부분 문자열
        if re.search(r"[A-Za-z]", kw):
            if re.search(rf"\b{re.escape(k_upper)}\b", en):
                return kw
        else:
            if kw in ko:
                return kw
    return None


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

    필터링:
    - 로컬 파일 존재하지 않으면 제외 (쿠팡 pull 실패)
    - 이미지 **가로/세로 500px 미만**이면 제외 (쿠팡 최소 스펙 위반 반려)
    """
    import os
    from PIL import Image

    with get_db() as conn:
        rows = conn.execute(
            """SELECT public_url, local_path FROM image_cache
               WHERE product_id=? AND public_url IS NOT NULL
               ORDER BY image_idx ASC""",
            (product_id,),
        ).fetchall()
    base = PUBLIC_BASE_URL.rstrip("/")
    urls = []
    for r in rows:
        pu = r["public_url"]
        lp = r["local_path"]
        if not pu:
            continue
        if lp and not os.path.isfile(lp):
            continue
        # 500x500 최소 스펙 검증 — Coupang 상세 이미지 요구사항
        if lp:
            try:
                with Image.open(lp) as im:
                    w, h = im.size
                if w < 500 or h < 500:
                    continue
            except Exception:
                # 이미지 열기 실패 → 불안정한 파일, 제외
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
        cat_path_row = conn.execute(
            "SELECT path FROM coupang_categories WHERE code=? LIMIT 1",
            (listing["coupang_category_code"] if listing else None,),
        ).fetchone() if listing else None

    raw_name = (p["title_ko"] or p["title_en"] or "").strip()
    name = _clean_product_name(raw_name)
    brand = _extract_brand(raw_name)
    price = int(listing["sale_krw"]) if listing and listing["sale_krw"] else int(p["sale_price_krw"] or 0)
    # 매핑 실패 시 "0" 으로 대체 — 쿠팡 자동 카테고리 매칭 기능에 위임.
    # 계정이 자동매칭 동의 상태(check-auto-category-agreed=true) 이므로 0 전송 시
    # 쿠팡이 상품 제목/이미지 기반으로 적절한 displayCategoryCode 를 할당한다.
    # 단 속성값이 비면 노출제한 상태로 등록되므로 Tier 2(메타 기반 속성 채움) 이후 완전 해결.
    category = str(listing["coupang_category_code"]) if listing and listing["coupang_category_code"] else "0"
    cat_path = cat_path_row["path"] if cat_path_row else ""

    # 건강기능식품 카테고리면 상품명에서 효능 표현 strip (자율심의 미통과 보수적 차단).
    # 광고문구 자체를 등록 페이로드에 넣으면 식약처 행정처분 위험.
    if _is_health_food_category(cat_path):
        stripped, claims = _strip_efficacy_claims(name)
        if claims:
            logger.info(
                f"[coupang] product {product_id} 건강식품 카테고리 효능 표현 strip — "
                f"매칭: {claims}, 원본: '{name}' → '{stripped}'"
            )
            name = stripped

    if image_urls is None:
        image_urls = _get_product_images(product_id)

    ok, err = _validate_payload(name, price, category, len(image_urls))
    if not ok:
        logger.warning(f"[coupang] product {product_id} 검증 실패: {err}")
        return None

    # 카테고리 메타 prefetch — "0"(자동매칭 대상) 은 실제 카테고리가 아니므로 메타 조회 스킵.
    meta = get_category_meta(category) if category != "0" else None
    notices = build_default_notices(meta) if meta else []
    if meta:
        attributes, skip_reason = build_required_attributes(meta, dict(p), cat_path=cat_path)
        if skip_reason:
            # 속성 추출 실패 — 페이로드 자체를 만들지 말고 skip 신호 반환.
            # list_product가 이를 감지해 excluded 처리.
            return {"_skip": skip_reason}
    else:
        # 자동매칭 대상(category="0") 혹은 메타 조회 실패 — 빈 attributes 로 전송.
        # 쿠팡이 자동 카테고리 매칭 후 필수속성 필드를 생성하되, 값은 비어있어 노출제한됨.
        # Tier 2 에서 Gemini 기반 속성값 추출로 채울 예정.
        attributes = []

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
        # 쿠팡 정책: 초도반품배송비 + 반품배송비 ≤ 판매가. 15000 × 2 = 30000 이므로 판매가
        # 30000 이하 상품은 고정값 시 에러. 각각 판매가의 절반 미만으로 캡하여 보정.
        "deliveryChargeOnReturn": min(P.COUPANG_RETURN_FEE, max(1000, price // 2 - 500)),
        "remoteAreaDeliverable": "N",
        "unionDeliveryType": "NOT_UNION_DELIVERY",
        "returnCenterCode": COUPANG_RETURN_CENTER_CODE,
        "returnChargeName": P.RETURN_CHARGE_NAME,
        "companyContactNumber": P.RETURN_CONTACT_NUMBER,
        "returnZipCode": P.RETURN_ZIP_CODE,
        "returnAddress": P.RETURN_ADDRESS,
        "returnAddressDetail": P.RETURN_ADDRESS_DETAIL,
        "returnCharge": min(P.COUPANG_RETURN_FEE, max(1000, price // 2 - 500)),
        "outboundShippingPlaceCode": COUPANG_OUTBOUND_SHIPPING_PLACE_CODE,
        "vendorUserId": COUPANG_USER_ID or COUPANG_VENDOR_ID,  # WING 로그인 계정 ID
        "requested": True,  # 즉시 승인 요청 — 쿠팡 심사 대기열로 전송
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
            "attributes": attributes,
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

    # ── 중복 ASIN 검사 (clean_policy) ──
    with get_db() as conn:
        asin_row = conn.execute("SELECT asin FROM products WHERE id=?", (product_id,)).fetchone()
    asin = asin_row['asin'] if asin_row else None
    if asin:
        is_dup, dup_info = clean_policy.check_duplicate_asin(asin, channel='coupang', exclude_product_id=product_id)
        if is_dup:
            reason = f"중복 ASIN — 이미 listed (product_id={dup_info['product_id']}, cpid={dup_info['channel_product_id']})"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='coupang'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_coupang', violation_type='duplicate_asin',
                action_taken='excluded', asin=asin,
                product_id=product_id, channel='coupang',
                notes=f'기존 listed product_id={dup_info["product_id"]}',
            )
            return {"ok": False, "skip": True, "error": reason}

    # 브랜드 블랙리스트 사전 차단 (쿠팡 유통경로 소명 대응 — 정품 민감 브랜드 차단)
    with get_db() as conn:
        prow = conn.execute(
            "SELECT title_en, title_ko FROM products WHERE id=?",
            (product_id,),
        ).fetchone()
    if prow:
        matched = _is_brand_blocked(prow["title_en"] or "", prow["title_ko"] or "", _load_brand_blocklist())
        if matched:
            reason = f"브랜드 블랙리스트 차단 ({matched})"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='coupang'""",
                    (reason, product_id),
                )
            return {"ok": False, "skip": True, "error": reason}

        # 국내 의약품 분류·식약처 금지 성분 hard block (clean_policy 위임)
        blocked_ing, ing = clean_policy.check_prohibited_ingredients(
            prow["title_en"] or "", prow["title_ko"] or "",
        )
        if blocked_ing:
            reason = f"금지 성분 차단 ({ing}) — 국내 의약품 분류 또는 수입금지"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='coupang'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_coupang', violation_type='prohibited_ingredient',
                action_taken='excluded', matched_keyword=ing,
                product_id=product_id, channel='coupang',
                original_text=prow['title_en'],
            )
            return {"ok": False, "skip": True, "error": reason}

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

    # build_payload가 _skip 반환하면 pre-API 단계에서 excluded 처리
    if isinstance(payload, dict) and payload.get("_skip") and "displayCategoryCode" not in payload:
        with get_db() as conn:
            conn.execute(
                """UPDATE listings_pa SET status='excluded', error_message=?,
                   last_synced_at=CURRENT_TIMESTAMP
                   WHERE product_id=? AND channel='coupang'""",
                (payload["_skip"], product_id),
            )
        return {"ok": False, "skip": True, "error": payload["_skip"]}

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
    # payload 에 requested=True 로 업로드했으므로 쿠팡 측에서 즉시 승인 프로세스가 시작됨.
    # approval_requested_at 를 같이 기록해야 "일괄 승인요청" 대상에서 제외되어 중복 404 호출을 막는다.
    with get_db() as conn:
        conn.execute(
            """UPDATE listings_pa SET channel_product_id=?, status='listed',
               approval_requested_at=CURRENT_TIMESTAMP,
               last_synced_at=CURRENT_TIMESTAMP, error_message=NULL
               WHERE product_id=? AND channel='coupang'""",
            (seller_product_id, product_id),
        )
        _sync_product_status(conn, product_id)
    return {"ok": True, "result": result}
