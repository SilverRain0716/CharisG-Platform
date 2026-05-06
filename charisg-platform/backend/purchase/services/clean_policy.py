"""
clean_policy.py — 네이버/쿠팡 클린 위반 방지 공용 정책 모듈.

3중 게이트 (sourcing → ai → upload) 의 단일 진입점.
모든 금지 키워드/효능 표현/카테고리/속성 정책은 이 파일에서만 관리한다.

배경 (2026-05-04):
  스마트스토어 클린위반 157건 적발 (중복 150 + 허위과대광고 3 + 취급불가 4).
  기존 coupang_lister.py 의 BANNED_INGREDIENT_KEYWORDS / EFFICACY_CLAIM_PATTERNS 를
  공용 모듈로 추출하고, 네이버 + 소싱 + AI 후처리에도 동일 정책 적용.

사용 예:
    from backend.purchase.services import clean_policy

    # 입구 (sourcing_promote)
    blocked, kw = clean_policy.check_prohibited_ingredients(title_en, title_ko)
    if blocked:
        clean_policy.log_violation(stage='sourcing', violation_type='prohibited_ingredient',
                                    matched_keyword=kw, asin=asin, action='blocked')
        continue

    # AI 후처리 (ai_processor) — 건강식품만
    if clean_policy.is_health_food_category(category_path):
        title_ko = clean_policy.sanitize_efficacy_claims(title_ko)

    # 업로드 (smartstore_lister / coupang_lister)
    dup, info = clean_policy.check_duplicate_asin(asin, channel='smartstore', exclude_product_id=pid)
    if dup:
        return {'ok': False, 'skip': True, 'error': f'중복 ASIN ({info["channel_product_id"]})'}
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 1. 금지 성분 (Prohibited Ingredients) — Hard Block
# ═══════════════════════════════════════════════════════════
# 매칭 시 등록 차단. 영업등록 / 식약처 신고와 무관하게 한국 수입금지 또는 의약품 분류.
# Tier 1: 마약류 / 향정신성
# Tier 2: 의약품 원료 (한국 처방 의약품)
# Tier 3: 식약처 미인정 원료
# Tier 4: 한국 수입 완전 금지 (비-성분)
# Tier 5: malltail 통관 거부 사례
PROHIBITED_INGREDIENTS = (
    # ── Tier 1: 마약류 / 향정신성 ──
    "Kratom", "크라톰",
    "Ephedra", "에페드라", "ephedrine", "에페드린", "마황",
    "CBD", "Cannabidiol", "칸나비디올",
    "THC", "tetrahydrocannabinol",
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
    "PABA", "파바", "Para-Aminobenzoic Acid",
    # ── Tier 3: 식약처 미인정 원료 ──
    "Shilajit", "실라짓", "쉬라짓",
    # ── 2026-05-04 쿠팡 DENIED 분석 기반 추가 ──
    "Zicam", "동종요법", "Homeopathic", "homeopathic",
    "Slippery Elm", "슬리퍼리엘름", "슬리퍼리 엘름",
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
    "PQQ", "Pyrroloquinoline",
    # ── 비만 약물 ──
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
    "sword", "blade", "도검",
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
    # ── Tier 6: 항공 운송 제한 / 배터리 (UN 38.3) ──
    # 리튬 배터리/배터리 내장 제품은 국제 항공 운송 제한 → 통관 자체 거부
    "lithium battery", "lithium-ion", "li-ion battery",
    "리튬 배터리", "리튬배터리", "리튬이온", "리튬-이온",
    "rechargeable battery", "충전식 배터리", "충전배터리",
    "power bank", "powerbank", "보조배터리",
    "battery pack", "battery-powered", "battery operated",
    "lithium polymer", "Li-Po", "리튬폴리머", "리튬 폴리머",
)


# ═══════════════════════════════════════════════════════════
# 2. 효능 표현 (Efficacy Claims) — Sanitize (건강식품 카테고리만)
# ═══════════════════════════════════════════════════════════
# 식약처 「건강기능식품 표시·광고 심의기준」 위반 우려 표현.
# 자율심의 미통과 시 의약품적 효능 표현은 금지.
EFFICACY_CLAIM_PATTERNS = (
    r"면역(?:력)?\s*(강화|증진|향상|개선|지원)",
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
    # ── 2026-05-04 신규 추가 (적발 사례 기반) ──
    r"설사\s*(완화|개선|예방)",
    r"알레르기\s*(완화|개선|예방|항)",
    r"감기\s*(예방|개선)",
    r"항\s*염증",
    r"면역\s*기능\s*(지원|강화|증진)",
    r"신경\s*(안정|진정)",
    r"근육\s*(이완|회복)",
    r"건강\s*보조",  # "장 건강 보조"
    r"기능\s*지원",  # "면역 기능 지원"

    # ── 2026-05-20 시행 식품 허위과대광고 정책 강화 (네이버 공지 2026-04-20) ──
    # 질병명 + 완치/치료/회복 결합 표현 — 1회 경고, 2회 제재, 3회 이용정지
    r"(?:당뇨|고혈압|비만|불면증?|우울증?|변비|설사|위염|위궤양|역류성?\s*식도염|"
    r"과민성?\s*대장|관절염|류마티스|골다공증|디스크|천식|비염|알레르기|아토피|"
    r"건선|습진|심장병|동맥경화|뇌졸중|치매|알츠하이머|간염|지방간|담석|"
    r"신장병|방광염|전립선염|요로결석|빈혈|백혈병|암|종양|갱년기|생리통|"
    r"자궁근종|난임|탈모|백반증|감기|독감|인후염|편도선염|두통|편두통|"
    r"어지럼증|기관지염|폐렴|결핵|공황장애)\s*(?:완치|치료|회복|개선|낫|치유|예방)",

    # 질병/증상 + "에 좋은/효능"
    r"(?:당뇨|고혈압|관절|간|위|장|신장|혈관|심장|뇌|폐|피부|머리|눈)"
    r"\s*(?:에\s*좋은|효능|효과)",

    # 한약 처방명 (단독 사용도 의약품 오인)
    r"(?:십전대보탕|보중익기탕|사물탕|사군자탕|육미지황탕|팔미지황탕|"
    r"우황청심원|천왕보심단|쌍화탕|갈근탕|소시호탕|인삼양영탕|생맥산|"
    r"공진단|경옥고|총명탕|보약)",

    # 의약품 오인 — 약효 단정 표현
    r"약효",
    r"치유\s*효과",
    r"의학적\s*효능",
    r"임상\s*증명",
)
_EFFICACY_RE = re.compile("|".join(EFFICACY_CLAIM_PATTERNS), re.IGNORECASE)


# ═══════════════════════════════════════════════════════════
# 3. 건강식품 카테고리 식별 (효능 필터 적용 대상)
# ═══════════════════════════════════════════════════════════
HEALTH_FOOD_CATEGORY_KEYWORDS = (
    "건강기능식품", "건강식품", "영양제", "보충제", "프로틴",
    "비타민", "오메가", "유산균", "프로바이오틱", "비오틴",
    "콜라겐", "마그네슘", "철분", "아연", "효소", "코큐텐",
    "단백질파우더", "다이어트식품", "건강분말", "건강즙",
    "한방재료", "환자식", "영양보충식", "숙취해소",
)


# ═══════════════════════════════════════════════════════════
# 4. 취급 불가 카테고리 (Prohibited Categories) — Hard Block
# ═══════════════════════════════════════════════════════════
PROHIBITED_CATEGORIES = (
    "성인용품", "성인",
    "주류", "와인", "맥주", "위스키",
    "담배", "전자담배", "니코틴",
    "도검", "총기", "에어건", "모조총",
    "마약", "대마",
    "의약품",
)


# ═══════════════════════════════════════════════════════════
# 도서 — 오디오북 / ebook 차단 (구매대행 부적합)
# ═══════════════════════════════════════════════════════════
# 종이책은 통과, 오디오북/전자책은 차단.
# 구매대행 = 실물 배송이 본질이라 디지털 콘텐츠는 처리 불가.
PROHIBITED_PRODUCT_TYPES = (
    "ABIS_EBOOKS",                  # Kindle eBooks
    "ABIS_AUDIO_BOOK",              # 일반 오디오북
    "AUDIBLE_AUDIO_EDITION",        # Audible 오디오북
    "DOWNLOADABLE_AUDIO_BOOK",      # 다운로드 오디오북
    "DOWNLOADABLE_VIDEO",           # 디지털 비디오
    "DOWNLOADABLE_MUSIC_TRACK",     # 디지털 음원
    "DIGITAL_VIDEO_GAMES",          # 디지털 게임
    "DIGITAL_SOFTWARE",             # 디지털 소프트웨어
    "DIGITAL_DEVICE_3",             # 디지털 기기 (Kindle 등)
    "PRESSED_AUDIO_BOOK",           # CD 오디오북 (애매하니 차단)
)

# title 키워드 fallback (productType 못 받았을 때)
DIGITAL_BOOK_TITLE_KEYWORDS = (
    "Audible Audiobook", "Audible Original", "Audible Edition",
    "Kindle Edition", "Kindle eBook", "Kindle Single",
    "eBook Edition", "Digital Edition",
    "오디오북", "전자책", "이북",
)


# ═══════════════════════════════════════════════════════════
# 5. 위반 이력 로그 (clean_violation_log 테이블)
# ═══════════════════════════════════════════════════════════
def log_violation(
    stage: str,
    violation_type: str,
    action_taken: str,
    matched_keyword: Optional[str] = None,
    product_id: Optional[int] = None,
    asin: Optional[str] = None,
    channel: Optional[str] = None,
    original_text: Optional[str] = None,
    notes: Optional[str] = None,
) -> None:
    """clean_violation_log 테이블에 위반 이력 기록.

    스키마 누락 시 silently 실패 (마이그레이션 전 호환성).
    """
    try:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO clean_violation_log
                   (stage, violation_type, action_taken, matched_keyword,
                    product_id, asin, channel, original_text, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (stage, violation_type, action_taken, matched_keyword,
                 product_id, asin, channel,
                 (original_text or "")[:500] if original_text else None,
                 notes),
            )
    except Exception as e:
        logger.warning(f"[clean_policy] 위반 로그 기록 실패: {e}")


# ═══════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════

def check_prohibited_ingredients(
    title_en: str = "",
    title_ko: str = "",
    description: str = "",
) -> tuple[bool, Optional[str]]:
    """상품명/설명에 금지 성분이 있는지 검사.

    Returns:
        (True, matched_keyword) — 차단 대상
        (False, None) — 통과
    """
    haystack = f"{title_ko or ''} {title_en or ''} {description or ''}"
    haystack_upper = haystack.upper()
    for kw in PROHIBITED_INGREDIENTS:
        if not kw:
            continue
        if re.search(r"[A-Za-z]", kw):
            # 영문은 단어 경계 검사 (false positive 방지)
            if re.search(rf"\b{re.escape(kw.upper())}\b", haystack_upper):
                return True, kw
        else:
            if kw in haystack:
                return True, kw
    return False, None


def check_prohibited_category(category_path: str) -> tuple[bool, Optional[str]]:
    """카테고리 경로에 취급불가 카테고리가 있는지 검사."""
    if not category_path:
        return False, None
    for kw in PROHIBITED_CATEGORIES:
        if kw in category_path:
            return True, kw
    return False, None


def check_prohibited_book(
    title_en: str = "",
    product_type: str = "",
) -> tuple[bool, Optional[str]]:
    """도서 — 오디오북/ebook/디지털 콘텐츠 차단.

    Args:
        title_en: 영문 상품명 (productType 없을 때 fallback)
        product_type: SP-API 응답의 productType 값

    Returns:
        (True, '오디오북' or 'ebook' or matched keyword) — 차단
        (False, None) — 통과
    """
    # 1) productType 정확 매칭 (우선)
    if product_type:
        pt_upper = product_type.upper()
        if pt_upper in PROHIBITED_PRODUCT_TYPES:
            return True, product_type

    # 2) title 키워드 fallback
    if title_en:
        for kw in DIGITAL_BOOK_TITLE_KEYWORDS:
            if kw.lower() in title_en.lower():
                return True, kw

    return False, None


def is_health_food_category(category_path: str) -> bool:
    """건강식품 카테고리 여부 (효능 필터 적용 판단)."""
    if not category_path:
        return False
    return any(kw in category_path for kw in HEALTH_FOOD_CATEGORY_KEYWORDS)


def has_efficacy_claims(text: str) -> bool:
    """효능 표현 포함 여부 (검출만, 수정 안 함)."""
    if not text:
        return False
    return bool(_EFFICACY_RE.search(text))


def sanitize_efficacy_claims(text: str) -> str:
    """효능 표현 제거. 매칭 부분을 빈 문자열로 치환 후 공백 정리."""
    if not text:
        return text
    cleaned = _EFFICACY_RE.sub("", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s*[,，]\s*[,，]\s*", ", ", cleaned)  # ", ," → ","
    cleaned = re.sub(r"^[,，\s]+|[,，\s]+$", "", cleaned)    # 앞뒤 콤마 제거
    return cleaned


def check_duplicate_asin(
    asin: str,
    channel: str,
    exclude_product_id: int,
) -> tuple[bool, Optional[dict]]:
    """같은 ASIN 이 다른 product_id 로 이미 listed 상태로 등록돼 있는지 검사.

    Returns:
        (True, {'product_id': ..., 'channel_product_id': ...}) — 중복
        (False, None) — 통과
    """
    if not asin:
        return False, None
    with get_db() as conn:
        row = conn.execute(
            """SELECT l.product_id, l.channel_product_id
               FROM listings_pa l
               JOIN products p ON l.product_id = p.id
               WHERE p.asin = ?
                 AND l.channel = ?
                 AND l.status = 'listed'
                 AND l.product_id != ?
               LIMIT 1""",
            (asin, channel, exclude_product_id),
        ).fetchone()
    if row:
        return True, dict(row)
    return False, None


def ensure_overseas_tag(name: str, max_len: int = 50) -> str:
    """상품명 앞에 [해외] 태그를 자동 부여. 50자 제한 유지.

    이미 [해외] 또는 유사 태그가 있으면 그대로 반환.
    """
    if not name:
        return name
    name = name.strip()
    # 이미 해외 관련 태그가 있는지 확인
    if re.match(r"^\s*\[(해외|구매대행|직배송|병행수입)\]", name):
        return name[:max_len]
    tag = "[해외] "
    available = max_len - len(tag)
    if available <= 0:
        return name[:max_len]
    return tag + name[:available]


_NUMERIC_UNIT_RE = re.compile(r"^\s*\d+\.?\d*\s*[a-zA-Z가-힣%]+\s*$")


def validate_numeric_attribute(value: str) -> str:
    """범위형 속성 값에서 단위/텍스트 제거. 숫자(.) 만 남김.

    "100ml" → "100"
    "250 g" → "250"
    "ABC" → "" (모두 제거되면 빈 문자열)
    "100" → "100" (변경 없음)
    """
    if not value:
        return value
    value = str(value).strip()
    if not _NUMERIC_UNIT_RE.match(value):
        return value  # 숫자+단위 패턴이 아니면 손대지 않음
    cleaned = re.sub(r"[^0-9.]", "", value)
    cleaned = re.sub(r"\.{2,}", ".", cleaned)  # ".." → "."
    cleaned = cleaned.strip(".")
    return cleaned


def sanitize_attribute_dict(attr_dict: dict) -> dict:
    """단일 attribute dict 의 attributeValue 정제.

    네이버 inferred_attributes_json 형식: {'attributeSeq': N, 'attributeValueSeq': M, 'attributeValue': '...'}
    """
    if not isinstance(attr_dict, dict):
        return attr_dict
    val = attr_dict.get("attributeValue")
    if isinstance(val, str):
        cleaned = validate_numeric_attribute(val)
        if cleaned != val:
            attr_dict = {**attr_dict, "attributeValue": cleaned}
    return attr_dict
