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
import unicodedata
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
    COUPANG_ACTIVE,
    PUBLIC_BASE_URL,
)

logger = logging.getLogger(__name__)


def _active_acct() -> str:
    """현재 활성 쿠팡 계정('old'|'new') — 정적 COUPANG_ACTIVE 대신 contextvar 인식.
    멀티계정(coupang_account("new"))에서 정적상수가 구계정에 고정되던 버그 해소."""
    from backend.purchase.services.coupang_service import active_account
    return active_account()


# ── 상품명 정리 (스마트스토어 lister와 동일 규칙) ────────────
_SPECIAL_CHAR_MAP = {
    '"': '인치', '\u201c': '인치', '\u201d': '인치',
    '*': 'x', '\\': ' ', '?': ' ', '<': '(', '>': ')',
    '\u00ae': '', '\u2122': '', '\u2120': '',   # ® ™ ℠ 상표기호 제거 (2026-07-23)
    '\u2013': ' ', '\u2014': ' ',                # – — 엠/엔대시 → 공백 (아마존 스타일 제거)
}
_SPECIAL_RE = re.compile('[' + re.escape(''.join(_SPECIAL_CHAR_MAP.keys())) + ']')


_BRAND_PLACEHOLDER_RE = re.compile(r'\[\s*브랜드[^\]]*\]\s*')


def _truncate_at_word(s: str, limit: int) -> str:
    """단어 중간 잘림 방지 절단 (2026-07-23). limit 초과 시 마지막 어절 경계에서 자름.
    한 단어가 너무 길어 60% 미만 지점에서만 공백이 나오면 하드컷(과절단 방지)."""
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    sp = cut.rfind(' ')
    if sp >= int(limit * 0.6):
        cut = cut[:sp]
    return cut.rstrip(" ,-\u2013\u2014·")


def _clean_product_name(name: str) -> str:
    # AI 가 출력한 [브랜드명], [브랜드 명], [브랜드명 미포함] 등 placeholder 제거
    name = _BRAND_PLACEHOLDER_RE.sub('', name or '')
    def _replace(m):
        return _SPECIAL_CHAR_MAP.get(m.group(0), ' ')
    cleaned = _SPECIAL_RE.sub(_replace, name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return _truncate_at_word(cleaned, P.MAX_PRODUCT_NAME_LEN)


def build_seller_product_name(title_ko: str | None, brand: str | None,
                                title_en: str | None = None,
                                fallback_name: str | None = None,
                                max_len: int = 80) -> str:
    """sellerProductName 결정 — placeholder 제거 + brand prefix 보강.

    1) title_ko 의 [브랜드명] placeholder 제거 (없으면 fallback_name → title_en).
    2) brand 가 영문이고 cleaned name 에 없으면 prefix 로 prepend.
    3) max_len 으로 절단.

    빈 결과면 빈 문자열 반환 (caller 가 빌드 거부 판단).
    """
    raw = title_ko or fallback_name or title_en or ""
    cleaned = _clean_product_name(raw)
    if brand:
        b = brand.strip()
        # 영문/숫자 위주 brand 만 prefix (한글/Generic 등 모호값 제외).
        if b and b.lower() not in ("generic", "n/a", "unknown") \
           and re.match(r"^[A-Za-z][A-Za-z0-9 .'\-]{1,}$", b) \
           and b.lower() not in cleaned.lower():
            cleaned = f"{b} {cleaned}".strip()
    return _truncate_at_word(cleaned, max_len)


def _extract_brand(name: str) -> str:
    words = name.split()
    if words and re.match(r'^[A-Za-z]', words[0]) and len(words[0]) >= 2:
        brand = words[0]
        if len(words) > 1 and re.match(r'^[A-Za-z]', words[1]) and len(words[1]) >= 2:
            brand = f"{words[0]} {words[1]}"
        return brand[:30]
    return "해외 브랜드"


# ── 쿠팡 searchTags 정규화 ──────────────────────────────────
# 쿠팡 검색은 형태소 자동 분리 → "남성티셔츠" 만 등록해도 "남성"/"티셔츠" 검색 모두 잡힘.
# 합성어 분해 추가는 키워드 스태핑 패널티 위험이라 하지 않는다 (쿠팡 마켓플레이스 공식).
_COUPANG_TAG_ALLOWED_PUNCT = set("!@#$%^&*-+;:'.")
# 2026-05-21 — 쿠팡 검색어는 [한글/ASCII 영숫자/공백/허용 특수문자] 만 통과.
# 이전 ch.isalnum() 은 'é', 'ü', 'ö' 등 라틴 확장도 True 반환 → 쿠팡이 "검색어 형식" 으로 거부.
# Acmé / Müv / König 같은 비ASCII 라틴 문자가 포함된 태그가 통째로 잘려 빈 태그면 skip.
_COUPANG_TAG_VALID_CHAR_RE = re.compile(r"[A-Za-z0-9가-힣ㄱ-ㅎㅏ-ㅣ]")


_BRAND_TAG_BLOCKLIST = {
    "샤넬", "구찌", "루이비통", "에르메스", "프라다", "디올", "버버리", "발리", "펜디",
    "셀린느", "보테가", "발렌시아가", "생로랑", "입생로랑", "몽클레어", "발렌티노",
    "지방시", "베르사체", "페라가모", "불가리", "까르띠에", "티파니", "롤렉스", "오메가",
    "코치", "마이클코어스", "토리버치", "고야드", "막스마라", "버버리체크", "명품백",
    "나이키", "아디다스", "뉴발란스", "퓨마", "리복", "컨버스", "반스", "언더아머",
    "아식스", "노스페이스", "파타고니아", "룰루레몬", "휠라", "챔피온",
    "chanel", "gucci", "louis vuitton", "hermes", "prada", "dior", "burberry", "bally",
    "nike", "adidas", "new balance", "puma", "reebok", "rolex",
}


def _normalize_search_tags(seo_tags_json: str | None, brand: str | None) -> list[str]:
    """seo_tags JSON + brand 영문 → 쿠팡 searchTags 배열.

    규격: 각 ≤20자, 최대 20개, !@#$%^&*-+;:'. 외 특수문자 제거, dedup.
    한글/ASCII 영숫자 외 문자(é, ü, ö 등) 는 제거 — 쿠팡이 "검색어 형식" 으로 reject.
    brand 가 ASCII 영문이면 1개 prepend (쿠팡 영/한 혼합 권장 부합).
    """
    out: list[str] = []
    seen: set[str] = set()

    def _allowed(ch: str) -> bool:
        # 화이트리스트 — 한글, ASCII 영숫자, 공백, 허용 특수문자만.
        if _COUPANG_TAG_VALID_CHAR_RE.match(ch):
            return True
        if ch.isspace():
            return True
        if ch in _COUPANG_TAG_ALLOWED_PUNCT:
            return True
        return False

    _own = (brand or "").strip().lower()
    _bblock = set(_BRAND_TAG_BLOCKLIST)
    if False and brand:  # ★브랜드 prepend 제거 (쿠팡: 노브랜드 상품 브랜드키워드 금지)
        b = brand.strip()
        if b and re.fullmatch(r"[A-Za-z][A-Za-z0-9 .'\-]*", b):
            cleaned = b[:20]
            key = cleaned.lower()
            if key not in seen:
                seen.add(key)
                out.append(cleaned)

    try:
        tags = json.loads(seo_tags_json) if seo_tags_json else []
    except (json.JSONDecodeError, TypeError):
        tags = []
    if not isinstance(tags, list):
        tags = []

    for raw in tags:
        if not isinstance(raw, str):
            continue
        cleaned = "".join(ch for ch in raw if _allowed(ch))
        # 연속 공백 단일화 + trim
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            continue
        # 라틴 확장 제거로 모든 영숫자가 사라진 케이스 — 안전 skip.
        if not _COUPANG_TAG_VALID_CHAR_RE.search(cleaned):
            continue
        cleaned = cleaned[:20]
        key = cleaned.lower()
        if key in _bblock:
            continue
        if _own and (key == _own or key.startswith(_own + " ")):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= 20:
            break

    return out


# ── 상세페이지 정적 배너 (Charis G 브랜드/배송/반품 안내) ──
# scripts/render_coupang_banners.py 로 한 번 렌더한 정적 JPG를 전 상품 공통 첨부.
# 내용 수정이 필요하면 templates/coupang_banners_src/*.html 수정 → 스크립트 재실행.
STATIC_BANNER_PATHS = (
    "/api/pa/images/banners/banner_1_brand.jpg",
    "/api/pa/images/banners/banner_2_shipping.jpg",
    "/api/pa/images/banners/banner_3_amazon.jpg",
    "/api/pa/images/banners/banner_4_purchase_notice.jpg",
)

# ★상세 최상단 관세 안내 배너(디자인 이미지) — 관세 대상 여부 비명시(마케팅)
CUSTOMS_BANNER_PATH = "/api/pa/images/banners/banner_5_customs.jpg"


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


# 목록통관 면세 한도 — 원가(cost_usd, USD) 초과 시 관세 발생 → 구매대행 부적합으로 등록/옵션 제외.
# 미국 한미FTA는 $200이나 보수적으로 전 원산지 $150 일괄 적용(2026-06-20, 사용자 지정).
CUSTOMS_DUTY_FREE_USD = 150.0


def _exceeds_customs_limit(cost_usd) -> bool:
    """원가가 목록통관 면세 한도(CUSTOMS_DUTY_FREE_USD) 초과면 True. None/파싱불가는 False(통과)."""
    try:
        return cost_usd is not None and float(cost_usd) > CUSTOMS_DUTY_FREE_USD
    except (TypeError, ValueError):
        return False


def _strip_accents(v: str) -> str:
    """악센트/움라우트 제거 (ü→u, é→e). Grüns==Gruns 매칭용 (2026-07-23)."""
    if not v:
        return ""
    return "".join(c for c in unicodedata.normalize("NFKD", v) if not unicodedata.combining(c))


def _is_brand_blocked(title_en: str, title_ko: str, blocklist: tuple[str, ...]) -> Optional[str]:
    """title에 블랙리스트 브랜드 키워드가 있으면 매칭된 키워드 반환.
    ★2026-07-23: 영문은 악센트 정규화(ü→u) 후 매칭 → 'Grüns' 하나로 'Gruns' 도 잡음."""
    en = _strip_accents((title_en or "").upper())
    ko = title_ko or ""
    for kw in blocklist:
        if not kw:
            continue
        k_upper = _strip_accents(kw.upper())
        # 영문은 단어 경계 검사, 한글은 부분 문자열
        if re.search(r"[A-Za-z]", kw):
            if re.search(rf"\b{re.escape(k_upper)}\b", en):
                return kw
        else:
            if kw in ko:
                return kw
    return None


# ── IP/총판 브랜드 필터 (화장품/건기식 전용, 2026-07-23) ────────────────────
#   지재권 신고가 화장품/건기식에 집중 → 이 카테고리에서만 총판 브랜드 차단.
#   전역 _is_brand_blocked 와 별개(타 카테고리 오차단 방지). 3채널(쿠팡구/신·네이버) 공용.
_IP_WATCHLIST_CACHE = None
_COSMETIC_SUPP_CODES = None


def _load_ip_watchlist() -> list[dict]:
    """brand_ip_watchlist 로드 (canonical + variants). 캐시."""
    global _IP_WATCHLIST_CACHE
    if _IP_WATCHLIST_CACHE is not None:
        return _IP_WATCHLIST_CACHE
    rows = []
    try:
        with get_db() as c:
            for r in c.execute("SELECT canonical, variants_json FROM brand_ip_watchlist"):
                try:
                    variants = json.loads(r["variants_json"] or "[]")
                except Exception:
                    variants = []
                terms = [r["canonical"]] + [v for v in variants if v]
                rows.append({"canonical": r["canonical"], "terms": tuple(terms)})
    except Exception as e:
        logger.warning(f"[ip-watchlist] 로드 실패: {e}")
    _IP_WATCHLIST_CACHE = rows
    return rows


def _cosmetic_supp_codes() -> set:
    """화장품/미용 + 식품>건강식품/다이어트 카테고리코드 집합. 캐시."""
    global _COSMETIC_SUPP_CODES
    if _COSMETIC_SUPP_CODES is not None:
        return _COSMETIC_SUPP_CODES
    codes = set()
    try:
        with get_db() as c:
            for r in c.execute(
                "SELECT CAST(id AS TEXT) id FROM naver_categories "
                "WHERE whole_name LIKE '화장품/미용%' OR whole_name LIKE '식품>건강식품%' "
                "   OR whole_name LIKE '식품>다이어트%'"
            ):
                codes.add(r["id"])
    except Exception as e:
        logger.warning(f"[ip-watchlist] 카테고리코드 로드 실패: {e}")
    _COSMETIC_SUPP_CODES = codes
    return codes


def check_ip_brand_blocked(category_path, title_en: str, title_ko: str, brand: str = "") -> Optional[str]:
    """IP/총판 브랜드 차단 — 화장품/건기식 카테고리일 때만 발동. 매칭 canonical 반환 or None.
    3채널 import 경로(list_product·smartstore_lister) 공용."""
    if str(category_path or "") not in _cosmetic_supp_codes():
        return None  # 화장품/건기식 아니면 IP필터 미적용
    wl = _load_ip_watchlist()
    if not wl:
        return None
    hay_en = _strip_accents((f"{title_en} {brand}").upper())
    hay_ko = f"{title_ko} {brand}"
    for entry in wl:
        for term in entry["terms"]:
            if not term:
                continue
            if re.search(r"[A-Za-z]", term):
                if re.search(rf"\b{re.escape(_strip_accents(term.upper()))}\b", hay_en):
                    return entry["canonical"]
            else:
                if term in hay_ko:
                    return entry["canonical"]
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


def _amazon_img_id(url: str) -> str:
    """Amazon 이미지 URL → 이미지 ID(크기변형·확장자 제거). 교차색상 공용이미지 매칭용."""
    base = (url or "").rsplit("/", 1)[-1]
    return re.split(r"[._]", base)[0] if base else ""


def _get_product_images(product_id: int, exclude_original_ids=None, strict_exclude=False) -> list[str]:
    """상품 이미지 URL 목록 — public_url을 PUBLIC_BASE_URL 절대 경로로 변환해 반환.

    필터링:
    - 로컬 파일 존재하지 않으면 제외 (쿠팡 pull 실패)
    - 양변 모두 500px 미만이면 제외 (방어용; 정상 신규는 image_downloader 가
      이미 1000x1000 으로 보정하므로 여기 걸릴 일 없음)
    """
    import os
    from PIL import Image

    with get_db() as conn:
        rows = conn.execute(
            """SELECT public_url, local_path, original_url FROM image_cache
               WHERE product_id=? AND public_url IS NOT NULL
               ORDER BY image_idx ASC""",
            (product_id,),
        ).fetchall()
    base = PUBLIC_BASE_URL.rstrip("/")
    # ★저작권 게이트 — lifestyle(초상권)+marketing(저작권) 제외. ★2026-07-25: 화장품/건기식(self_made)만 엄격.
    #   일반상품은 완전완화(lifestyle·marketing 포함 전 이미지 사용) — IP 신고가 화장품/건기식에 집중되기 때문.
    #   일반상품은 비전분류(Gemini)도 스킵 → 등록 속도↑.
    _strict_img = (_image_policy(product_id) == "self_made")
    if _strict_img:
        try:
            from backend.purchase.services.image_classifier import classify_images
            _img_cls = classify_images(product_id)
        except Exception as _e:
            logger.warning(f"[coupang] 이미지 분류 실패(필터 생략) product={product_id}: {_e}")
            _img_cls = {}
    else:
        _img_cls = {}   # 일반상품: 전 이미지 사용(완전완화)
    urls = []
    _kept_lp = []          # urls 와 1:1 정렬된 local_path (교차색상 게이트용)
    _mk_urls = []          # 마케팅으로 분류돼 제외된 URL (전부 제외 시 대표 폴백용)
    _excluded_shared = []  # 교차색상 공용이미지로 제외된 클린 URL (전부 제외 시 폴백)
    for r in rows:
        pu = r["public_url"]
        lp = r["local_path"]
        if not pu:
            continue
        if lp and not os.path.isfile(lp):
            continue
        # 이미지 사이즈 검증 — 한 변이라도 500px 이상이면 통과(완화 게이트).
        # ⚠️ 쿠팡 실제 규칙은 **양변 모두 ≥500** 이며 단변<500 은 노출제한이 아니라
        #    "승인반려" 처리됨(2026-06-01 458건 중 97% 이 사유로 확인). 과거엔
        #    "등록은 받는다"고 잘못 알아 게이트를 max≥500 으로 완화했던 것이 대량 반려 원인.
        # 근본 해결은 상류(image_downloader._normalize_for_coupang)가 1000x1000 으로
        # 보정하는 것. 여기 게이트는 구(舊) 비보정 이미지의 빈-페이로드 회귀를 막기 위해
        # 완화 상태 유지. 기존 이미지 백필 완료 후 min(w,h)<500 으로 강화 권장.
        if lp:
            try:
                with Image.open(lp) as im:
                    w, h = im.size
                # 쿠팡 규칙: 갤러리(DETAIL) 이미지는 양변 모두 ≥500. 단변<500이면 "기타이미지" 반려.
                # (구 max(w,h)<500 완화 게이트가 400x500 등 단변<500을 통과시켜 승인반려 유발 → min 으로 강화)
                if min(w, h) < 500:
                    continue
            except Exception:
                # 이미지 열기 실패 → 불안정한 파일, 제외
                continue
        _u = pu if pu.startswith("http") else f"{base}{pu}"
        if lp and _img_cls.get(lp, "photo") != "photo":
            _mk_urls.append(_u)          # marketing(그래픽)+lifestyle(인물/실사용환경) — 저작권·초상권 위험, 갤러리 제외
            continue
        # ★ 교차색상 공용이미지 제외 — 색상 무관 generic(여러 색상 자식이 공유) 이미지는
        #   옵션 색상과 불일치(화이트 옵션에 흑색 hero 등). 그 자식의 색상전용 이미지를 쓰도록 제외.
        if exclude_original_ids:
            _oid = _amazon_img_id(r["original_url"] or "")
            if _oid and _oid in exclude_original_ids:
                _excluded_shared.append(_u)
                continue
        urls.append(_u)
        _kept_lp.append(lp)
    if _mk_urls:
        logger.info(f"[coupang] product {product_id} 갤러리: 마케팅 그래픽 {len(_mk_urls)}장 제외(저작권)")
    # ★교차색상 게이트(2026-06-29): 같은 ASIN 갤러리에 섞인 '다른 색 변형 누끼컷'을 상세에서 제외.
    #   메인(첫 이미지=판매색)과 색이 크게 다른 '깨끗한 누끼컷'만 제거. 라이프스타일/다이어그램
    #   (배경 있음=흰테두리 낮음)은 색이 달라도 유지(오제거 방지). 아마존이 한 자식 갤러리에
    #   전 색상 이미지를 담는 탓에 발생(예 콜맨 의자: 그레이 판매인데 레드/블루/블랙 누출).
    if len(urls) > 1 and _kept_lp and _kept_lp[0]:
        try:
            import numpy as _np
            from PIL import Image as _IM

            def _pcol(_lp):
                _im = _IM.open(_lp)
                try:
                    _im.draft("RGB", (96, 96))  # ★경량화: 디코딩 시점 축소
                except Exception:
                    pass
                _im = _im.convert("RGB").resize((96, 96))
                _a = _np.asarray(_im).reshape(-1, 3).astype(int)
                _mx = _a.max(1); _mn = _a.min(1)
                _pr = _a[~((_mn >= 210) & ((_mx - _mn) <= 25))]
                return _np.median(_pr, axis=0) if len(_pr) >= 150 else None

            _main = _pcol(_kept_lp[0])
            if _main is not None:
                _fu, _fl = [urls[0]], [_kept_lp[0]]
                for _u2, _lp2 in zip(urls[1:], _kept_lp[1:]):
                    _drop = False
                    if _lp2:
                        try:
                            if _border_white_ratio(_IM.open(_lp2)) >= 0.5:  # 깨끗한 누끼컷만 색 비교
                                _c = _pcol(_lp2)
                                if _c is not None and float(_np.linalg.norm(_c - _main)) > 60:
                                    _drop = True
                        except Exception:
                            _drop = False
                    if not _drop:
                        _fu.append(_u2); _fl.append(_lp2)
                if len(_fu) < len(urls):
                    logger.info(f"[coupang] product {product_id} 교차색상 누끼 {len(urls) - len(_fu)}장 제외(상세 판매색만)")
                urls = _fu; _kept_lp = _fl
        except Exception as _e:
            logger.warning(f"[coupang] 교차색상 게이트 스킵 product={product_id}: {_e}")
    # ★중복 이미지 제거(2026-06-29): 같은 갤러리에 동일/거의동일 이미지가 다른 idx로 들어가
    #   상세에 같은 사진이 반복되는 문제(예 265 패드·279 불멍컷). average hash 해밍거리<=10 = 중복.
    if len(urls) > 1:
        try:
            from PIL import Image as _DI
            def _ahash(_lp, n=16):
                _im = _DI.open(_lp).convert("L").resize((n, n))
                _px = list(_im.getdata()); _av = sum(_px) / len(_px)
                return [1 if v > _av else 0 for v in _px]
            _seen = []; _du = []; _dl = []
            for _u, _lp in zip(urls, _kept_lp):
                _h = None
                try:
                    if _lp: _h = _ahash(_lp)
                except Exception:
                    _h = None
                _dup = bool(_h) and any(sum(a != b for a, b in zip(_h, _ph)) <= 10 for _ph in _seen)
                if _dup:
                    continue
                if _h: _seen.append(_h)
                _du.append(_u); _dl.append(_lp)
            if len(_du) < len(urls):
                logger.info(f"[coupang] product {product_id} 중복이미지 {len(urls) - len(_du)}장 제거")
            urls = _du; _kept_lp = _dl
        except Exception as _e:
            logger.warning(f"[coupang] 중복제거 스킵 product={product_id}: {_e}")
    if strict_exclude:
        # ★strict: 색상전용 photo 만 반환([] 가능). 호출측이 같은 색상 형제 이미지 차용 트리거.
        return urls
    if not urls and _excluded_shared:
        # 색상전용 이미지 0장(전부 공용) — 제외하면 0장 → 공용이미지 폴백(없는 것보단 나음)
        logger.info(f"[coupang] product {product_id} 색상전용 이미지 없음 — 공용이미지 {len(_excluded_shared)}장 폴백")
        urls = _excluded_shared
    if not urls and _mk_urls:
        # 클린 0장 — 대표 식별을 위해 1장만 폴백(없으면 등록 자체 불가)
        logger.warning(f"[coupang] product {product_id} 클린 갤러리 0장 — 대표 1장 폴백")
        return _mk_urls[:1]
    return urls


# ── 대표(누끼형) 이미지 선택 ─────────────────────────────────
# 사용자 정책(2026-06-19): 갤러리 대표는 아마존 원본 다수가 아니라 가장 흰배경에 가까운
# 제품컷 1장만(누끼형). 추가 갤러리는 미사용 — 상세 contents 가 제품 이미지 커버.

def _border_white_ratio(im, strip: float = 0.10) -> float:
    """테두리 픽셀이 흰색(>=238)에 가까운 비율 — 흰배경 제품컷일수록 높음."""
    try:
        im.draft("RGB", (400, 400))  # ★경량화(2026-07-25): 디코딩 시점 저해상도 — 결과 동일, 디코딩 ~2배 빠름
    except Exception:
        pass
    im = im.convert("RGB")
    w, h = im.size
    px = im.load()
    sw, sh = max(1, int(w * strip)), max(1, int(h * strip))
    pts = white = 0
    for x in range(0, w, 4):
        for y in list(range(0, sh)) + list(range(h - sh, h)):
            r, g, b = px[x, y]; pts += 1
            if r >= 238 and g >= 238 and b >= 238: white += 1
    for y in range(0, h, 4):
        for x in list(range(0, sw)) + list(range(w - sw, w)):
            r, g, b = px[x, y]; pts += 1
            if r >= 238 and g >= 238 and b >= 238: white += 1
    return white / max(1, pts)


def select_representative_image(product_id: int) -> Optional[str]:
    """대표(누끼형) 1장 — 마케팅 제외, 가장 흰배경 제품컷을 흰배경보정해 media 저장,
    절대 public_url 반환. 실패 시 None."""
    import os
    from PIL import Image
    from pathlib import Path as _Prep
    # ★ 합성전용 상품: AI 테마컷(themed_cut > design_cut)을 대표로 우선 사용 (저작권 디자인)
    _repmd = _Prep(__file__).resolve().parent.parent / "media" / "products" / str(product_id)
    for _rn in ("themed_cut.jpg", "design_cut.jpg"):
        if (_repmd / _rn).is_file():
            return f"{PUBLIC_BASE_URL.rstrip('/')}/api/pa/images/products/{product_id}/{_rn}"
    try:
        from backend.purchase.services.detail_infographic import _classify_plain, _whiten
    except Exception:
        def _classify_plain(_im): return False
        def _whiten(_im): return _im
    try:
        from backend.purchase.services.image_classifier import classify_images
        _cls = classify_images(product_id)
    except Exception:
        _cls = {}
    with get_db() as conn:
        rows = conn.execute(
            "SELECT local_path FROM image_cache WHERE product_id=? ORDER BY image_idx",
            (product_id,),
        ).fetchall()

    def _score(only_size=False):
        out = []
        for r in rows:
            lp = r["local_path"]
            if not lp or not os.path.isfile(lp):
                continue
            if not only_size and _cls.get(lp, "photo") != "photo":
                continue   # marketing+lifestyle 제외(only_size 폴백은 무관, 최후 대표 확보용)
            try:
                im = Image.open(lp).convert("RGB")
                if min(im.size) < 500:
                    continue
                out.append((lp, _classify_plain(im), _border_white_ratio(im)))
            except Exception:
                continue
        return out

    cands = _score() or _score(only_size=True)
    if not cands:
        return None
    # ★색상 일치(2026-06-29): 상세(_get_product_images)는 image_idx 순으로 메인(img_000)을 리드로
    #   쓰는데, 과거 대표는 idx 무시하고 '가장 흰배경'만 골라 갤러리에 섞인 타색 변형(스와치)을
    #   대표로 뽑아 대표색≠상세색 불일치 발생(예 Naturehike 의자: 대표 블랙 vs 상세 카키).
    #   _score 는 rows(image_idx ASC) 순서를 보존하므로, 메인(cands[0])이 plain 이면 메인을 대표로
    #   써서 상세 리드와 동일색을 보장. 메인이 plain 이 아닐 때만 첫 plain → 없으면 메인.
    # ★색상 일치 v2(2026-06-29): plain 우선을 제거하고 cands[0](상세 리드=메인 img_000)를 그대로
    #   대표로 사용. plain 우선은 메인이 non-plain일 때 타색 plain 이미지로 새어 대표색≠상세색을
    #   다시 유발했음(예 Grope 테이블: 대표 블루 vs 상세 블랙). _score 는 _get_product_images 와
    #   동일 필터(마케팅·size)·image_idx 순이라 cands[0] == 상세 리드 → 동일 이미지=동일색 보장.
    best_lp = cands[0][0]
    try:
        im = _whiten(Image.open(best_lp).convert("RGB"))
        if im.size != (1000, 1000):
            im.thumbnail((1000, 1000))
            canvas = Image.new("RGB", (1000, 1000), (255, 255, 255))
            canvas.paste(im, ((1000 - im.width) // 2, (1000 - im.height) // 2))
            im = canvas
        from pathlib import Path as _P
        out = _P(best_lp).parent / "rep_nuki.jpg"
        im.save(out, "JPEG", quality=90, optimize=True)
    except Exception as e:
        logger.warning(f"[coupang] 대표컷 생성 실패 product={product_id}: {e}")
        return None
    return f"{PUBLIC_BASE_URL.rstrip('/')}/api/pa/images/products/{product_id}/rep_nuki.jpg"


def select_all_nuki_images(product_id: int, cap: int = 9,
                           exclude_original_ids=None, strict_exclude: bool = False) -> list:
    """제품사진(photo)으로 분류된 이미지 전부를 누끼(흰배경보정+1000패딩)로 자체 저장 → public_url 리스트.
    - 합성전용(themed/design cut): 그 AI컷 1장.
    - 분류 캐시 없으면(Gemini 미실행/실패) [] → 호출측이 대표1장 폴백(마케팅 누출 방지).
    저작권: 흰배경 기계적 제품컷(제품사진=저작권 무보호) 자체 가공본만 게시 (2026-07-05)."""
    import os
    from PIL import Image
    from pathlib import Path as _P
    base = PUBLIC_BASE_URL.rstrip("/")
    _md = _P(__file__).resolve().parent.parent / "media" / "products" / str(product_id)
    for _rn in ("themed_cut.jpg", "design_cut.jpg"):
        if (_md / _rn).is_file():
            return [f"{base}/api/pa/images/products/{product_id}/{_rn}"]
    try:
        from backend.purchase.services.image_classifier import _cache_path
        if not _cache_path(product_id).exists():
            return []   # 분류 미보유 → 멀티누끼 보류(안전, 마케팅 누출 방지)
    except Exception:
        return []
    # 필터(마케팅·라이프스타일·교차색상·중복·size)는 _get_product_images 에 위임
    urls = _get_product_images(product_id, exclude_original_ids, strict_exclude) or []
    if not urls:
        return []
    with get_db() as conn:
        rows = conn.execute(
            "SELECT public_url, local_path FROM image_cache WHERE product_id=?",
            (product_id,),
        ).fetchall()
    _lp_by_url = {}
    for r in rows:
        pu = r["public_url"]
        if not pu:
            continue
        _u = pu if pu.startswith("http") else f"{base}{pu}"
        _lp_by_url[_u] = r["local_path"]
    try:
        from backend.purchase.services.detail_infographic import _whiten
    except Exception:
        def _whiten(_im):
            return _im
    out = []
    for u in urls[:cap]:
        lp = _lp_by_url.get(u)
        if not lp or not os.path.isfile(lp):
            continue
        try:
            im = _whiten(Image.open(lp).convert("RGB"))
            if im.size != (1000, 1000):
                im.thumbnail((1000, 1000))
                canvas = Image.new("RGB", (1000, 1000), (255, 255, 255))
                canvas.paste(im, ((1000 - im.width) // 2, (1000 - im.height) // 2))
                im = canvas
            fn = "nuki_%d.jpg" % len(out)
            im.save(_md / fn, "JPEG", quality=90, optimize=True)
            out.append(f"{base}/api/pa/images/products/{product_id}/{fn}")
        except Exception:
            continue
    return out


# ── 페이로드 빌드 ──────────────────────────────────────────────

def _extract_barcode(identifiers_json) -> str:
    """products.identifiers_json([{"type":"EAN","value":...},...])에서 쿠팡 바코드 1개 추출.
    우선순위 EAN > UPC > GTIN, 8~14자리 숫자만. (2026-05-25: 기존 emptyBarcode 하드코딩 수정)"""
    if not identifiers_json:
        return ""
    try:
        arr = json.loads(identifiers_json)
    except Exception:
        return ""
    by_type = {}
    for x in (arr if isinstance(arr, list) else []):
        if not isinstance(x, dict):
            continue
        # ① 평탄형 {"type","value"}
        if x.get("value"):
            by_type[str(x.get("type") or "").upper()] = str(x["value"]).strip()
        # ② ★SP-API 원형 {"marketplaceId", "identifiers":[{"identifierType","identifier"}]}
        #    이걸 못 읽어서 EAN 보유 상품이 바코드 없이 등록되고 있었다(2026-08-13 실측).
        for y in (x.get("identifiers") or []):
            if isinstance(y, dict) and y.get("identifier"):
                t = str(y.get("identifierType") or "").upper()
                by_type.setdefault(t, str(y["identifier"]).strip())
    for t in ("EAN", "UPC", "GTIN"):
        v = by_type.get(t)
        if v and v.isdigit() and 8 <= len(v) <= 14:
            return v
    return ""


def _barcode_with_facts(asin: str, identifiers_json, product_id=None) -> str:
    """바코드 추출 — 비면 SP-API facts 를 먼저 확정하고 identifiers_json 을 재조회.

    ★2026-08-04 버그수정: 호출부가 products 행을 읽은 시점에 facts 미적재면
      identifiers_json 이 비어 GTIN 보유 상품이 emptyBarcode=True 로 등록되던 문제.
      (facts 는 뒤이어 _resolve_model_no 가 적재하므로 DB 에는 GTIN 이 남아 원인이 가려졌다)
    """
    bc = _extract_barcode(identifiers_json)
    if bc or not asin:
        return bc
    try:
        from backend.purchase.services.sp_api_facts import get_strict_facts
        _f = get_strict_facts(asin) or {}
        # facts dict 에 identifiers 가 있으면 그걸 먼저 쓴다(캐시히트로 DB 재기록이 없을 수 있음).
        _ids = _f.get("identifiers")
        if _ids:
            import json as _j
            bc = _extract_barcode(_ids if isinstance(_ids, str) else _j.dumps(_ids))
            if bc:
                return bc
        with get_db() as _c:
            if product_id:
                _r = _c.execute("SELECT identifiers_json FROM products WHERE id=?",
                                (product_id,)).fetchone()
            else:
                _r = _c.execute("SELECT identifiers_json FROM products WHERE asin=? LIMIT 1",
                                (asin,)).fetchone()
        if _r:
            bc = _extract_barcode(_r["identifiers_json"])
    except Exception as e:
        logger.warning(f"[coupang] 바코드 재조회 실패 {asin}: {e}")
    return bc


def _predict_category(name: str, brand: str = "") -> tuple[str, str]:
    """쿠팡 ML 카테고리 추천 (categorization/predict). 자동매칭 대상(category=0)에 실 카테고리 부여용.

    반환: (predictedCategoryId, predictedCategoryName) — 실패 시 ("", "").
    신규 계정은 category=0(빈 속성) 등록을 거부하므로, 실 카테고리를 확정해 필수속성을 채운다.
    """
    try:
        from backend.purchase.services import coupang_service as cs
        path = "/v2/providers/openapi/apis/api/v1/categorization/predict"
        body = {"productName": name[:100], "brand": brand or "", "productDescription": ""}
        r = cs._request_with_retry("POST", cs.BASE + path,
                                   headers=cs._signature("POST", path), json=body, timeout=20)
        if r is None:
            return "", ""
        d = (r.json().get("data") or {})
        if str(d.get("autoCategorizationPredictionResultType")) == "SUCCESS" and d.get("predictedCategoryId"):
            return str(d.get("predictedCategoryId")), str(d.get("predictedCategoryName") or "")
    except Exception as e:
        logger.warning(f"[coupang] 카테고리 예측 실패: {e}")
    return "", ""


def _resolve_brand_ko(eng_brand: str) -> str:
    """영문 브랜드 → settings.coupang.brand_ko_map 의 쿠팡 표준 한글명(없으면 영문 유지).
    무브랜드(Generic 등)는 '비브랜드'(상품명 정책). 금지 특수문자 제거(아포/하이픈 유지)."""
    eng = (eng_brand or "").strip()
    # ★신규계정: WING 미등록 브랜드명은 brandId 없이 거부됨(memory ②, 2026-06-20 PINMEI 실패 실측).
    # WING 브랜드관리에 등록된 브랜드만 settings.coupang.brand_registered 화이트리스트에 추가 → 한글표준명 허용. 나머지 전부 노브랜드("").
    if _active_acct() == "new":
        try:
            with get_db() as c:
                _rr = c.execute("SELECT value FROM settings WHERE key='coupang.brand_registered'").fetchone()
            _reg = set(json.loads(_rr["value"])) if (_rr and _rr["value"]) else set()
        except Exception:
            _reg = set()
        if eng not in _reg:
            return ""   # 노브랜드 강제(브랜드 키워드는 searchTags로 검색성 유지)
    ko = eng
    try:
        with get_db() as c:
            r = c.execute("SELECT value FROM settings WHERE key='coupang.brand_ko_map'").fetchone()
        if r and r["value"]:
            ko = (json.loads(r["value"]).get(eng) or eng)
    except Exception:
        pass
    # 쿠팡 brand 규격: 띄어쓰기·특수문자 없이 (상품등록 API 문서). 한글/영문 표준명.
    ko = re.sub(r"[!$?_{}\^*%@#~<>\[\]|=]", "", (ko or "")).replace(" ", "").strip()
    if not ko or ko.lower() in ("generic", "unknown", "n/a", "unbranded", "no brand", "비브랜드", "노브랜드"):
        return "비브랜드"
    return ko


_NO_BRAND_TOKENS = {"generic", "unknown", "n/a", "unbranded", "no brand", "비브랜드", "노브랜드", ""}

# 미매칭 브랜드 폴백 (2026-07-23): 쿠팡 brandId 조회 실패 시 노브랜드 대신 "아마존"(KR-37909)으로 등록.
#   근거: 사장 방침 — 무명/미등록 브랜드도 판매자 신뢰도상 노브랜드보다 "아마존"이 낫다.
#   "아마존"(KR-37909, isUIDRequired=False)은 GTIN/바코드 불필요 → 등록에 지장 없음.
#   ★(a)브랜드없음 + (b)브랜드명 있으나 라이브러리 미스, 둘 다 적용. 단 정확/음역 매치가 되면 그 실브랜드 우선.
# ★2026-08-08: 한글 "아마존"(KR-37909) → 영문 "amazon"(KR-110982) 로 교체(사장 지시).
#   쿠팡 브랜드 라이브러리에 둘 다 존재하며 UID 불필요는 동일. 실측 확인함.
_AMAZON_BRAND = {"brand_id": "KR-110982", "brand_name": "amazon", "uid_required": False}


def _translit_brand_ko(eng: str) -> str:
    """영문 브랜드명 → 한글 음역(소리나는 대로). ★뜻 번역 아님(음역). 실패 시 ''.
    쿠팡 브랜드 라이브러리는 한글명으로 등록돼 있어(유토피아타월=KR-30786) 영문 정확매치가 안 됨 →
    음역 후 정확매치로 brandId를 찾기 위함. Gemini 1콜(브랜드당 캐시라 1회)."""
    import os as _os
    import requests as _rq
    eng = (eng or "").strip()
    if not eng:
        return ""
    keys = [_os.environ.get(k) for k in ("GEMINI_API_KEY_5", "GEMINI_API_KEY_FALLBACK", "GEMINI_API_KEY", "GEMINI_API_KEY_2")]
    keys = [k for k in keys if k]
    if not keys:
        return ""
    prompt = (
        "다음 영문 브랜드명을 한국어로 '소리나는 대로'(음역)만 변환한다. ★뜻 번역 절대 금지. "
        "한글 브랜드명만 한 줄로 출력(설명·따옴표·영문 없이).\n"
        "예: Utopia Towels→유토피아타월, Chef Works→셰프웍스, RumbleRoller→럼블롤러, PROFOOT→프로풋, Ellie Home→엘리홈\n"
        + eng)
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "thinkingConfig": {"thinkingBudget": 0}}}
    for k in keys:
        try:
            r = _rq.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={k}",
                         json=body, timeout=30)
            if r.status_code == 200:
                t = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip().strip('"').split("\n")[0].strip()
                if t and any("가" <= c <= "힣" for c in t):
                    return t[:50]
        except Exception:
            pass
    return ""


# ── 상품명 B(좁은 브랜드 음역) ─────────────────────────────────
# 한국에서 영문으로 통용되는 브랜드 → 상품명에서 음역 안 함(BMW를 비엠더블유로 쓰지 않음).
_BRAND_KEEP_EN = {
    "bmw", "audi", "benz", "mercedes", "volkswagen", "vw", "toyota", "honda", "ford", "tesla",
    "apple", "iphone", "ipad", "macbook", "airpods", "samsung", "lg", "sony", "nike", "adidas",
    "puma", "reebok", "3m", "hp", "dell", "asus", "msi", "intel", "amd", "nvidia", "gopro",
    "dyson", "bosch", "makita", "dewalt", "logitech", "anker", "jbl", "bose", "canon", "nikon",
    "lego", "ikea", "philips", "xiaomi", "lenovo", "acer", "audi", "kia", "hyundai",
}

_B_TL_PROMPT = (
    "다음 상품 브랜드명을 한국어로 음역(발음대로 한글표기)해라. 발음 가능하면 자연스러운 한글"
    "(komestone→콤스톤, Spigen→슈피겐, Owala→오왈라). 순수 자음약자나 숫자 위주 코드처럼 음역이 "
    "무의미하면 영문 원본 그대로. 한글 음역 또는 영문, 딱 한 줄만 출력. 설명·따옴표·기호 금지.\n브랜드: "
)


def _brand_translit_cached(brand: str) -> str:
    """브랜드 한글 음역 (brand_ko_cache 캐시). 약자/코드로 음역 무의미하면 '' (호출측이 영문 유지)."""
    import os as _os
    import requests as _rq2
    b = (brand or "").strip()
    if not b or b.lower() in _NO_BRAND_TOKENS:
        return ""
    try:
        with get_db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS brand_ko_cache (brand TEXT PRIMARY KEY, korean TEXT)")
            row = c.execute("SELECT korean FROM brand_ko_cache WHERE brand=?", (b,)).fetchone()
        if row is not None:
            return row["korean"] or ""
    except Exception:
        pass
    keys = [_os.environ.get(k) for k in ("GEMINI_API_KEY_5", "GEMINI_API_KEY_FALLBACK", "GEMINI_API_KEY", "GEMINI_API_KEY_2")]
    keys = [k for k in keys if k]
    out = ""
    body = {"contents": [{"parts": [{"text": _B_TL_PROMPT + b}]}],
            "generationConfig": {"temperature": 0, "thinkingConfig": {"thinkingBudget": 0}}}
    for k in keys:
        try:
            r = _rq2.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={k}",
                          json=body, timeout=30)
            if r.status_code == 200:
                cand = r.json().get("candidates")
                if cand:
                    t = cand[0]["content"]["parts"][0]["text"].strip().strip('"').split("\n")[0].strip()
                    # 한글이 있으면 음역 성공, 없으면(영문 그대로 반환=약자) '' → 영문 유지
                    out = t[:50] if any("가" <= c <= "힣" for c in t) else ""
                    # ★글자단위 스펠링(WWHSAI→더블유더블유...) 가드: 음역이 원본보다 길면 약자 gibberish → 영문 유지
                    if out and len(out.replace(" ", "")) > len(b.replace(" ", "")):
                        out = ""
                    break
        except Exception:
            pass
    try:
        with get_db() as c:
            c.execute("INSERT OR REPLACE INTO brand_ko_cache(brand,korean) VALUES(?,?)", (b, out))
            c.commit()
    except Exception:
        pass
    return out


def apply_brand_translit_to_name(name: str, brand: str) -> str:
    """상품명 B: name이 brand로 시작 + 관용영문 아니면 앞 브랜드를 한글 음역으로 치환.
    브랜드 컬럼이 name 앞단과 불일치(액세서리 등)하거나 약자면 원본 그대로 유지(안전)."""
    if not name or not brand:
        return name
    b = brand.strip()
    if b.lower() in _BRAND_KEEP_EN:
        return name
    if not name.lower().startswith(b.lower()):
        return name
    ko = _brand_translit_cached(b)
    if not ko or ko == b or not any("가" <= c <= "힣" for c in ko):
        return name
    rest = name[len(b):].lstrip(" -,·")
    # ★중복 방지(2026-07-01): rest가 이미 같은 음역으로 시작하면(기존 title_ko에 음역 존재)
    #   영문 브랜드만 제거 — "Scotch 스카치 3밀" → "스카치 3밀"(스카치 스카치 방지).
    if rest.startswith(ko):
        return rest.strip()[:P.MAX_PRODUCT_NAME_LEN]
    return f"{ko} {rest}".strip()[:P.MAX_PRODUCT_NAME_LEN]


# ── 어린이/키즈 용어 완화 (KC 오탐 방지) ──────────────────
# ★어린이제품 카테고리(완구·유아·아동 등)는 진짜 어린이제품 → KC 적용, 손대지 않음(우회 금지).
#   비-어린이 카테고리에서 번역상 부수적으로 붙은 '어린이/키즈'만 완화(온가족용/전연령). (2026-07-01)
_KIDS_CAT_MARKERS = ("유아", "아동", "완구", "영유아", "출산", "어린이집", "영아", "키즈카페", "장난감")
_KIDS_DUAL = [
    (r"성인\s*[및/,]?\s*어린이(용|들)?", "온 가족"),
    (r"어린이\s*[및/,]?\s*성인(용|들)?", "온 가족"),
    (r"남녀노소(용)?", "온 가족용"),
    (r"아이\s*[및/,]?\s*어른(용)?", "온 가족"),
]


def soften_kids_terms(name: str, cat_path: str = "") -> str:
    """비-어린이 카테고리 상품명의 부수적 '어린이/키즈' → '온 가족용/전 연령' (KC 오탐 완화).
    ★어린이제품 카테고리면 원본 유지(KC 적용 대상 — 우회 안 함)."""
    if not name:
        return name
    if cat_path and any(m in cat_path for m in _KIDS_CAT_MARKERS):
        return name
    out = name
    for pat, rep in _KIDS_DUAL:
        out = re.sub(pat, rep, out)
    out = re.sub(r"어린이(용|들)?", "전 연령", out)
    out = re.sub(r"키즈", "전 연령", out)
    out = re.sub(r"\s+", " ", out).strip(" ,")
    return out[:P.MAX_PRODUCT_NAME_LEN]


def _resolve_brand_new(eng_brand: str) -> dict:
    """신규계정용 브랜드 해석 — 쿠팡 브랜드 라이브러리에서 brandId 조회(+캐시).

    2026-05 정책: 신규 API Key 발급 계정은 상품등록 시 brandId 필수(브랜드명만 보내면 반려).
    brand_search(이름)→brandId. 반환 {"brand_id":str|None, "brand_name":str, "uid_required":bool}.
      brand_id=None → 라이브러리에 없음(무명 셀러브랜드 등) = 노브랜드로 등록.
    캐시: coupang_brand_cache(brand_en PK). 미스시 1회 검색 후 저장(라이브러리 없음도 캐시해 재호출 방지).
    """
    eng = (eng_brand or "").strip()
    if not eng or eng.lower() in _NO_BRAND_TOKENS:
        # 브랜드 없음/일반명 → 아마존 폴백 (구: 노브랜드).
        return dict(_AMAZON_BRAND)
    try:
        with get_db() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS coupang_brand_cache (
                    brand_en TEXT PRIMARY KEY, brand_id TEXT, brand_name_ko TEXT,
                    uid_required INTEGER DEFAULT 0, synced_at TEXT)"""
            )
            row = c.execute(
                "SELECT brand_id, brand_name_ko, uid_required FROM coupang_brand_cache WHERE brand_en=?",
                (eng,),
            ).fetchone()
        if row is not None:
            return {"brand_id": row["brand_id"], "brand_name": row["brand_name_ko"] or "",
                    "uid_required": bool(row["uid_required"])}
    except Exception as e:
        logger.warning(f"[brand-new] 캐시 조회 실패 {eng}: {e}")
    try:
        from backend.purchase.services.coupang_service import brand_search
        hit = brand_search(eng)
    except Exception as e:
        logger.warning(f"[brand-new] 검색 실패 {eng}: {e}")
        hit = None
    if hit and hit.get("brandId"):
        res = {"brand_id": str(hit["brandId"]), "brand_name": str(hit.get("brandName") or ""),
               "uid_required": bool(hit.get("isUIDRequired"))}
    else:
        # 검색 실패 기본값 = 아마존 폴백 (구: 노브랜드). 아래 음역 정확매치 성공 시 실브랜드로 덮어씀.
        res = dict(_AMAZON_BRAND)
        # ★영문 라이브러리 미스 → 한글 음역 후 정확매치 (2026-06-30). 쿠팡 브랜드DB는 한글명으로
        #   등록돼 영문 정확매치가 안 됨(유토피아타월=KR-30786 등). 음역 후 정확매치는 안전(items[0]
        #   퍼지매치와 달리 오매칭 없음 — Lusso→디루소 같은 사고 방지). 띄어쓰기 변형도 시도.
        try:
            from backend.purchase.services.coupang_service import brand_search as _bs2
            _ko = _translit_brand_ko(eng)
            for _cand in [x for x in (_ko, _ko.replace(" ", "")) if x]:
                _h2 = _bs2(_cand)
                if _h2 and _h2.get("brandId"):
                    res = {"brand_id": str(_h2["brandId"]), "brand_name": str(_h2.get("brandName") or ""),
                           "uid_required": bool(_h2.get("isUIDRequired"))}
                    logger.info(f"[brand-new] {eng} 영문미스 → 음역 '{_cand}' 정확매치 brandId={res['brand_id']}")
                    break
        except Exception as _e:
            logger.warning(f"[brand-new] 음역 폴백 실패 {eng}: {_e}")
    try:
        with get_db() as c:
            c.execute(
                """INSERT OR REPLACE INTO coupang_brand_cache
                   (brand_en, brand_id, brand_name_ko, uid_required, synced_at)
                   VALUES (?,?,?,?, datetime('now'))""",
                (eng, res["brand_id"], res["brand_name"], 1 if res["uid_required"] else 0),
            )
            c.commit()
    except Exception as e:
        logger.warning(f"[brand-new] 캐시 저장 실패 {eng}: {e}")
    return res


def _gtin_attribute(barcode: str) -> dict:
    """UID필수 브랜드용 GTIN attribute (2026 신규 필수속성 포맷, exposed=NONE)."""
    return {"attributeTypeName": "Global Trade Item Number",
            "attributeValueName": str(barcode), "exposed": "NONE"}


def _resolve_model_no(asin: str) -> str:
    """GTIN 없는 상품의 품번(modelNo) — SP-API model_number/part_number 코드형 우선, 폴백 ASIN.
    서술형(상품명류)은 식별번호 정책 위반이라 제외(짧고 공백≤1 인 코드형만). (2026-08-01 정책)"""
    if not asin:
        return ""

    def _okv(v):
        v = (v or "").strip()
        # ★ASIN 은 아마존 내부 식별자다 — 식별번호로 쓸 수 없다(사장님 지시 2026-08-12)
        if not v or v.upper() == asin.upper():
            return None
        return v if (len(v) <= 30 and v.count(" ") <= 1) else None

    # ① DB 우선 — 임포트 파이프라인(M16)이 SP-API 원본에서 채워 둔다.
    #   라이브 조회에만 의존하면 한 번 흔들릴 때 ASIN 으로 떨어진다.
    try:
        with get_db() as _c:
            _r = _c.execute("SELECT sp_model_number, sp_part_number FROM products WHERE asin=?"
                            " AND (sp_model_number IS NOT NULL OR sp_part_number IS NOT NULL)"
                            " LIMIT 1", (asin,)).fetchone()
        if _r:
            for v in (_r["sp_model_number"], _r["sp_part_number"]):
                got = _okv(v)
                if got:
                    return got
    except Exception as e:
        logger.warning(f"[coupang] modelNo DB 조회 실패 {asin}: {str(e)[:60]}")

    # ② SP-API 라이브 폴백
    try:
        from backend.purchase.services.sp_api_facts import get_strict_facts
        f = get_strict_facts(asin) or {}
        for v in (f.get("model_number"), f.get("part_number")):
            got = _okv(v)
            if got:
                return got
    except Exception as e:
        logger.warning(f"[coupang] modelNo 조회 실패 {asin}: {e}")

    # ③ ★ASIN 폴백 없음 — 모델번호도 GTIN 도 없으면 등록이 막히는 게 맞다.
    logger.warning(f"[coupang] ★{asin} 모델번호 없음 — ASIN 대체 금지(사장님 지시). "
                   f"바코드도 없으면 등록 불가")
    return ""


import re as _re_repdup


def _media_local_for_url(url):
    """이미지 URL(.../products/{pid}/{file}) → 로컬 media 파일경로(존재 시). 아니면 None."""
    m = _re_repdup.search(r"/products/(\d+)/([^/?#]+\.(?:jpe?g|png))", str(url or ""), _re_repdup.I)
    if not m:
        return None
    from pathlib import Path as _P
    p = _P(__file__).resolve().parent.parent / "media" / "products" / m.group(1) / m.group(2)
    return str(p) if p.is_file() else None


def _img_ahash16(lp):
    from PIL import Image as _I
    im = _I.open(lp).convert("L").resize((16, 16))
    px = list(im.getdata()); av = sum(px) / len(px)
    return [1 if v > av else 0 for v in px]


def _drop_rep_duplicates(rep_url, detail_urls, thresh: int = 10):
    """★대표이미지(갤러리)와 동일/근접(average-hash 해밍≤thresh)한 상세 이미지를 제외.
    갤러리 대표와 상세 첫 컷이 같은 사진이라 중복 노출되던 문제(2026-06-30) 방지.
    rep 로컬파일을 못 찾으면 원본 그대로 반환(안전)."""
    if not rep_url or not detail_urls:
        return detail_urls
    rlp = _media_local_for_url(rep_url)
    if not rlp:
        return detail_urls
    try:
        rh = _img_ahash16(rlp)
    except Exception:
        return detail_urls
    out = []
    for u in detail_urls:
        lp = _media_local_for_url(u)
        if lp:
            try:
                if sum(a != b for a, b in zip(rh, _img_ahash16(lp))) <= thresh:
                    continue  # 대표와 중복 → 상세에서 제외
            except Exception:
                pass
        out.append(u)
    # ★상세가 통째로 비면(전부 대표와 중복=이미지 1장뿐인 상품 등) 원본 유지 — 이미지 없는 상세 방지.
    #   (대표↔상세 1회 겹침 < 상세 제품컷 0. 다중이미지는 정상 dedup, 단일이미지만 보존) (2026-06-30)
    if not out and detail_urls:
        return list(detail_urls)
    return out


def _ensure_single_editorial(pid):
    """단품 에디토리얼(사진 포함) 생성/캐시 → ed_manifest.json URL 리스트. 실패시 []. (2026-07-05 신규리스팅용)"""
    import subprocess as _sp, sys as _sy, os as _os, json as _sj
    from pathlib import Path as _P
    _rb = _P.home() / "CharisG-Platform/charisg-platform"
    _mf = _rb / "backend/purchase/media/products" / str(pid) / "ed_manifest.json"
    try:
        if not _mf.exists():
            _sp.run([_sy.executable, str(_rb / "scripts/migrate/render_editorial_runner.py"), str(pid)],
                    cwd=str(_rb), env={**_os.environ, "PYTHONPATH": str(_rb)}, timeout=180, capture_output=True, text=True)
        if _mf.exists():
            return _sj.loads(_mf.read_text()) or []
    except Exception:
        pass
    return []


# ── 상세 이미지 altText (2026-08-08) ─────────────────────────────────
#  장애인차별금지법·한국형 웹콘텐츠 접근성지침 2.0 대응. 종전에는 모든 이미지
#  블록의 altText 가 "" 라서 스크린리더에 아무 정보도 전달되지 않았다. 우리 상세는
#  글자를 전부 이미지 안에 넣기 때문에, alt 가 비면 시각장애인에게는 상세페이지가
#  통째로 존재하지 않는 것과 같다. 파일명 규칙으로 용도를 판별해 라벨을 붙인다.
#  ★배너 문구는 실제 렌더 결과를 읽고 맞춘 값이다. 배너 내용을 바꾸면 여기도 고칠 것
#    — 틀린 대체텍스트는 빈 대체텍스트보다 나쁘다.
_ALT_BANNER = {
    "banner_1_brand":           "정품 인증 안내 — 본 제품은 100% 정품입니다",
    "banner_2_shipping":        "해외배송 절차 안내",
    "banner_3_amazon":          "해외 구매대행 상품 안내",
    "banner_4_purchase_notice": "구매 전 확인사항 안내",
    "banner_5_customs":         "관세 안내 — 미화 150달러 초과 시 관부가세 별도",
}


def _alt_text_for(url, product_name: str = "") -> str:
    """이미지 URL → 대체텍스트. 파일명에 번호가 있으면 그대로 순번에 쓴다
    (호출측 카운터를 두지 않아도 블록마다 서로 다른 값이 나온다)."""
    s = str(url or "")
    for key, label in _ALT_BANNER.items():
        if key in s:
            return label
    nm = (product_name or "").strip()
    if len(nm) > 50:
        nm = nm[:50].rstrip() + "…"
    pre = (nm + " ") if nm else ""
    if "components_cut" in s:
        return (pre + "구성품 안내").strip()
    if "components_block" in s:
        return (pre + "구성품 상세").strip()
    if "/spec.jpg" in s:
        return (pre + "제품 사양표").strip()
    if "/infographic.jpg" in s:
        return (pre + "제품 특징 안내").strip()
    m = re.search(r"agent_sec(\d+)", s)
    if m:
        return (pre + f"상품 상세 이미지 {int(m.group(1)) + 1}").strip()
    m = re.search(r"img_(\d+)", s)
    if m:
        return (pre + f"상품 이미지 {int(m.group(1)) + 1}").strip()
    return (pre + "상품 이미지").strip()


def build_detail_contents(product_id: int, image_urls=None, fast: bool = False, shared_editorial=None) -> list:
    """상세 contents 구성(단일경로 build_payload 와 동일 템플릿) — 그룹 옵션별로도 재사용.
    순서: ① 브랜드 정품배너 ② seo_detail 매니페스트 OR (선별제품컷≤5 + 자체 인포그래픽) ③ 스펙표 ④ 정보배너.
    저작권 방어(자체제작 콘텐츠 + 실제 제품사진만). render_infographic/spec_table 은 캐시.

    fast=True(디테일링 분리): ②의 AI 선별(Gemini)·인포그래픽 렌더를 생략하고 전달된 실제
    제품컷(image_urls, 이미 분류·저작권필터됨)을 직접 사용 → 빠른 임시저장. 리치 상세는
    나중에 enrich 패스가 seo_detail.json 채우면 재등록/수정으로 반영.
    """
    import json as _json
    from pathlib import Path as _Path
    base = PUBLIC_BASE_URL.rstrip("/")
    image_urls = image_urls or []
    cp = []
    # altText 용 상품명 1회 조회 — 실패해도 라벨(배너/사양표/순번)은 그대로 나온다.
    try:
        with get_db() as _c:
            _r = _c.execute(
                "SELECT COALESCE(NULLIF(title_ko,''), title_en) AS nm FROM products WHERE id=?",
                (product_id,),
            ).fetchone()
        _alt_nm = (_r["nm"] if _r else "") or ""
    except Exception:
        _alt_nm = ""
    # ★대표이미지(갤러리)와 중복되는 상세 제품컷 제외용 — rep ahash 비교 (2026-06-30)
    # ★2026-08-03: 정책별로 "갤러리 대표"가 다른데 늘 누끼컷(select_representative_image)과
    #   비교하고 있었다. amazon 정책의 갤러리 대표는 _raw_amazon_images[0] 이므로 비교 대상이
    #   어긋나 중복 제거가 헛돌았다. 정책에 맞는 대표를 쓰도록 정정 —
    #   부수적으로 amazon 정책에서 비전분류(Gemini) 호출이 사라진다(전 품목 호출의 주경로였다).
    _rep_u = None
    try:
        if _image_policy(product_id) == "amazon":
            _raw_rep = _raw_amazon_images(product_id, 1)
            _rep_u = _raw_rep[0] if _raw_rep else None
        else:
            _rep_u = select_representative_image(product_id)
    except Exception:
        _rep_u = None
    def _b(u):
        return {"contentsType": "IMAGE_NO_SPACE",
                "contentDetails": [{"content": u, "detailType": "IMAGE",
                                    "altText": _alt_text_for(u, _alt_nm)}]}
    # ★관세 안내 배너(최상단)
    cp.append(_b(f"{base}{CUSTOMS_BANNER_PATH}"))
    # ① 브랜드 정품 배너
    cp.append(_b(f"{base}{STATIC_BANNER_PATHS[0]}"))
    if shared_editorial:  # ★그룹 옵션: 옵션사진 + 그룹당 1회 텍스트 에디토리얼 (2026-07-05)
        for _pu in _drop_rep_duplicates(_rep_u, list((image_urls or [])[:4])):
            cp.append(_b(_pu))
        for _su in shared_editorial:
            cp.append(_b(_su if str(_su).startswith("http") else f"{base}{_su}"))
    # ②③ seo_detail 매니페스트 우선, 없으면 선별제품컷 + 인포그래픽 폴백
    _md = _Path(__file__).resolve().parent.parent / "media" / "products" / str(product_id)
    _man = _md / "seo_detail.json"
    if not _man.exists():
        _man = _md / "ed_manifest.json"   # ★ prep 에디토리얼(저작권 디자인) 폴백 — fast모드에서도 적용
    _used = bool(shared_editorial)
    # ── 이미지 정책 분기(2026-07-06): 그 외 카테고리=아마존 원본(구계정 방식) ──
    if _image_policy(product_id) == "amazon":
        for _ru in _raw_amazon_images(product_id, 8):
            cp.append(_b(_ru))
        _used = True
    try:
        if not _used and _man.exists():
            _urls = _json.loads(_man.read_text())
            for _u in _urls:
                cp.append(_b(_u if str(_u).startswith("http") else f"{base}{_u}"))
            _used = bool(_urls)
    except Exception as _e:
        logger.warning(f"[coupang] seo manifest 실패(폴백) product={product_id}: {_e}")
    if not _used and fast:
        # ★빠른 모드(디테일링 분리): AI 선별·인포그래픽 생략, 실제 제품컷 직접 사용.
        for url in _drop_rep_duplicates(_rep_u, list(image_urls[:5])):
            cp.append(_b(url))
    elif not _used:
        try:
            from backend.purchase.services.detail_infographic import select_detail_images
            _sel = select_detail_images(product_id, max_n=5)
        except Exception as _e:
            logger.warning(f"[coupang] 이미지 선별 실패(폴백) product={product_id}: {_e}"); _sel = None
        # ★선별(plain) 이미지 + 부족분 보충 (2026-06-30) — select_detail_images 가 <3 장이면
        #   게이트통과(분류·마케팅제외) image_urls 로 5장까지 보충해 상세 휑함 방지(designed 이미지 유지).
        _detail_imgs = list(_sel or [])
        if len(_detail_imgs) < 3:
            for _u in (image_urls or []):
                if _u not in _detail_imgs:
                    _detail_imgs.append(_u)
                if len(_detail_imgs) >= 5:
                    break
        for url in _drop_rep_duplicates(_rep_u, list(_detail_imgs or image_urls[:5])):
            cp.append(_b(url))
        try:
            from backend.purchase.services.detail_infographic import render_infographic
            _ig = render_infographic(product_id)
            if _ig:
                cp.append(_b(f"{base}{_ig}"))
        except Exception as _e:
            logger.warning(f"[coupang] 인포그래픽 렌더 실패(계속) product={product_id}: {_e}")
    # ★구성품 가공컷(세트/번들) — components_cut.jpg 존재 시 '구성품' 전용 블록
    _cc2 = _Path(__file__).resolve().parent.parent / "media" / "products" / str(product_id) / "components_cut.jpg"
    if _cc2.is_file():
        cp.append(_b(f"{base}/api/pa/images/products/{product_id}/components_cut.jpg"))
    _cb2 = _Path(__file__).resolve().parent.parent / "media" / "products" / str(product_id) / "components_block.jpg"
    if _cb2.is_file():
        cp.append(_b(f"{base}/api/pa/images/products/{product_id}/components_block.jpg"))
    # ④ 세부사항 스펙표
    try:
        from backend.purchase.services.spec_table import render_spec_table
        _spec = render_spec_table(product_id)
        if _spec:
            cp.append(_b(f"{base}{_spec}"))
    except Exception as _e:
        logger.warning(f"[coupang] 스펙표 렌더 실패(계속) product={product_id}: {_e}")
    # ⑤ 정보 배너(배송/아마존/필독)
    for rel in STATIC_BANNER_PATHS[1:]:
        cp.append(_b(f"{base}{rel}"))
    return cp


def _image_policy(product_id: int) -> str:
    """이미지 정책 — 'self_made'(화장품/건강식품: 누끼/에디토리얼) | 'amazon'(그외: 원본)."""
    try:
        with get_db() as conn:
            row = conn.execute("SELECT image_policy, amazon_category_json FROM products WHERE id=?", (product_id,)).fetchone()
    except Exception:
        return "amazon"
    if not row:
        return "amazon"
    pol = row["image_policy"]
    if pol in ("self_made", "amazon"):
        return pol
    try:
        from backend.purchase.services import clean_policy as _cp
        pol = _cp.classify_image_policy(row["amazon_category_json"])
        with get_db() as conn:
            conn.execute("UPDATE products SET image_policy=? WHERE id=?", (pol, product_id))
            conn.commit()
        return pol
    except Exception:
        return "amazon"


def _raw_amazon_images(product_id: int, cap: int = 9) -> list:
    """아마존 원본 제품이미지(구계정 방식) — image_cache 그대로(마케팅 필터 없음), size>=500."""
    import os
    from PIL import Image
    base = PUBLIC_BASE_URL.rstrip("/")
    out = []
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT public_url, local_path FROM image_cache WHERE product_id=? AND public_url IS NOT NULL ORDER BY image_idx",
                (product_id,)).fetchall()
    except Exception:
        return out
    # 2026-08-03: 대표(첫 장)는 규격 미달이어도 버리지 않는다.
    #   기존에는 500px 미만이면 조용히 건너뛰어 두 번째/중간 컷이 대표로 올라갔다.
    #   image_idx 0 = 아마존 MAIN 이 되도록 SP-API 로 사전 정렬해 둔다(img_reorder).
    for _i, r in enumerate(rows):
        if len(out) >= cap:
            break
        pu = r["public_url"]; lp = r["local_path"]
        if lp and os.path.isfile(lp) and _i > 0:
            try:
                with Image.open(lp) as im:
                    if min(im.size) < 500:
                        continue
            except Exception:
                continue
        out.append(pu if str(pu).startswith("http") else f"{base}{pu}")
    return out


def _has_hangul(v) -> bool:
    return bool(v) and any('\uac00' <= ch <= '\ud7a3' for ch in str(v))


def check_upload_ready(product_id: int, row=None) -> tuple[bool, str]:
    """업로드 게이트 (2026-07-23) — 단품·그룹 공통. 미완성 상품(특히 미번역 영문)이
    쿠팡에 올라가는 것을 원천 차단. 실패=보류(excluded 아님) → 다음 사이클 재시도.

    필수: ①title_ko 한글 ②sale_price_krw>0 ③images_json ④seo_tags ⑤sp_api_facts_at.
      - ⑥brand: 미매칭도 아마존 폴백(KR-37909)으로 항상 해결 → 별도 차단 안 함.
      - ⑨가격상세: 옵션A 채택 → ②(sale_price>0)로 충분.
      - ⑪필수속성: build_payload 내 build_required_attributes _skip 로 이미 강제됨.
    ①은 title_ko 기준(그룹 마스터명·단품 폴백명이 모두 title_ko를 쓰므로 가장 안전).
    반환: (True,"") | (False,"gate:<사유>").
    """
    if row is None:
        with get_db() as _c:
            row = _c.execute(
                "SELECT title_ko, sale_price_krw, images_json, seo_tags, sp_api_facts_at "
                "FROM products WHERE id=?", (product_id,),
            ).fetchone()
        if row is None:
            return False, "gate:product_없음"
    def _g(k):
        try:
            return row[k]
        except Exception:
            return None
    if not _has_hangul(_g("title_ko")):
        return False, "gate:제목_미번역(영문/공백)"
    if not ((_g("sale_price_krw") or 0) > 0):
        return False, "gate:판매가_없음"
    if (_g("images_json") or "") in ("", "[]"):
        return False, "gate:이미지_없음"
    if (_g("seo_tags") or "") in ("", "[]"):
        return False, "gate:seo_tags_없음"
    if not _g("sp_api_facts_at"):
        return False, "gate:sp_api_facts_미적재"
    return True, ""


def build_payload(product_id: int, image_urls: list[str] | None = None,
                  requested: bool = False, force_no_brand: bool = False,
                  gallery_one: bool = False) -> Optional[dict]:
    # ★2026-08-08 기본값 True→False. 운영 불변규칙이 "판매요청 자동 안 함(임시저장까지만)"
    #   인데 기본값이 정반대라 호출측이 생략하면 즉시 승인요청까지 갔다.
    """쿠팡 sellerProducts POST 페이로드 빌드.

    force_no_brand=True: 신규계정에서 brandId 해석을 건너뛰고 노브랜드("")로 등록
    (게이팅 브랜드 등록실패 시 재시도용 폴백).

    ⚠️ Phase 0-3 (운영자 수동 등록 페이로드 캡처) 후 보정 필요.
    """
    with get_db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not p:
            return None
        listing = conn.execute(
            "SELECT sale_krw, coupang_category_code, coupang_auto_matched "
            "FROM listings_pa WHERE product_id=? AND channel='coupang'",
            (product_id,),
        ).fetchone()
        cat_path_row = conn.execute(
            "SELECT path FROM coupang_categories WHERE code=? LIMIT 1",
            (listing["coupang_category_code"] if listing else None,),
        ).fetchone() if listing else None

    # ★SEO 최적화 제목(seo_title) 우선 — 황금공식·핵심키워드 앞배치 (2026-06-19). 없으면 title_ko 폴백.
    _tko = _ensure_title_ko(product_id, p["title_ko"], p["title_en"])  # ★등록시 한글제목 보장(2026-07-15)
    # ★seo_title 영문 방어(2026-07-23): seo_title 이 영문(SEO생성 실패/구버전)이면 한글 title_ko 우선.
    #   게이트는 title_ko 한글을 보장하나 이름소스가 seo_title 먼저라 영문 seo_title 이 그대로 등록되던 구멍(2,328건) 차단.
    _seo = (p["seo_title"] or "").strip()
    raw_name = (_seo if _has_hangul(_seo) else (_tko or _seo or p["title_en"] or "")).strip()
    name = _clean_product_name(raw_name)
    from backend.purchase.services.brand_normalizer import normalize_brand
    # 쿠팡 표준 브랜드명 (띄어쓰기·특수문자 제거, AI).
    # ★_extract_brand 폴백("해외 브랜드")은 실제 브랜드가 아님 → 빈 브랜드로 처리.
    #   (normalize_brand가 "해외 브랜드"를 AI로 "Amazon"으로 잘못 캐싱 → searchTags·manufacture 오염되던 버그 차단)
    _raw_brand = _extract_brand(raw_name)
    brand = "" if _raw_brand == "해외 브랜드" else normalize_brand(_raw_brand)
    _bc = _barcode_with_facts(p["asin"], p["identifiers_json"], product_id)
    # ★정상가/판매가 분리 (2026-07-06): original_price_krw NULL이면 그 자리에서 SP-API로 채움(lazy).
    #   신규·마이그 모두 그대로 추월. 수동잠금(listing.price_mode=manual)은 listing["sale_krw"]이 우선되므로 보존.
    _opk = None; _spk = p["sale_price_krw"]
    try:
        _opk = p["original_price_krw"]
    except Exception:
        _opk = None
    if not _opk and p["asin"] and not (listing and listing["sale_krw"]):
        try:
            from backend.purchase.services.dual_pricing import refresh_dual_price
            _r = refresh_dual_price(p["id"], p["asin"])
            if _r:
                _opk = _r["original_krw"]; _spk = _r["sale_krw"]
        except Exception:
            pass
    price = int(listing["sale_krw"]) if listing and listing["sale_krw"] else int(_spk or 0)
    try:
        _orig_price = int(_opk) if _opk else price
    except Exception:
        _orig_price = price
    if _orig_price < price:
        _orig_price = price
    # 매핑 실패 시 "0" 으로 대체 — 쿠팡 자동 카테고리 매칭 기능에 위임.
    # 계정이 자동매칭 동의 상태(check-auto-category-agreed=true) 이므로 0 전송 시
    # 쿠팡이 상품 제목/이미지 기반으로 적절한 displayCategoryCode 를 할당한다.
    # 단 속성값이 비면 노출제한 상태로 등록되므로 Tier 2(메타 기반 속성 채움) 이후 완전 해결.
    # coupang_auto_matched=1: 카테고리 매핑 confidence soft(50~70) 또는 옵션 검증 실패 후 강제 위임.
    # 이 경우 hard category 가 있어도 "0" 강제 → attributes=[] 로 자동분류 위임.
    _auto_match = bool(listing and listing["coupang_auto_matched"]) if listing else False
    if _auto_match:
        category = "0"
    else:
        category = str(listing["coupang_category_code"]) if listing and listing["coupang_category_code"] else "0"
    cat_path = cat_path_row["path"] if cat_path_row else ""

    # ★신규 계정(카리스 글로벌)은 category=0(빈 속성) 등록을 거부 — "필수 구매 옵션 없음".
    # 쿠팡 ML 예측으로 실 카테고리를 확정해 아래 메타기반 필수속성을 채운다.
    # 구 계정은 grandfathered(노출제한 등록 허용)라 기존 동작 유지.
    if category == "0" and _active_acct() == "new":
        _pid_code, _pid_name = _predict_category(name, brand)
        if _pid_code:
            category = _pid_code
            if not cat_path:
                with get_db() as _c2:
                    _cr = _c2.execute(
                        "SELECT path FROM coupang_categories WHERE code=? LIMIT 1", (_pid_code,)
                    ).fetchone()
                cat_path = (_cr["path"] if _cr else "") or _pid_name
            logger.info(f"[coupang] product {product_id} 카테고리 예측 확정 → {_pid_code}({_pid_name})")

    # 건강기능식품 카테고리면 상품명에서 효능 표현 strip (자율심의 미통과 보수적 차단).
    # 광고문구 자체를 등록 페이로드에 넣으면 식약처 행정처분 위험.
    # ★clean_policy 중앙 패턴 사용 (해외직구 식품 부당광고 강화, 2026-06-18 통일).
    if _is_health_food_category(cat_path):
        stripped = clean_policy.sanitize_efficacy_claims(name)
        if stripped != name:
            logger.info(
                f"[coupang] product {product_id} 건강식품 효능 표현 strip — "
                f"'{name}' → '{stripped}'"
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
        # ★Fix B(2026-07-27): 신규계정은 category=0(빈 속성) 등록을 "필수 구매 옵션 없음"으로 거부.
        #   등록 시도 시 실패하는데도 쿠팡 일일 5000 옵션 quota는 소모됨(하루 ~358건 낭비).
        #   → 신규계정이면 등록 대신 스킵(카테고리 해결 후 재등록 가능). 구계정은 자동매칭 유지.
        try:
            from backend.purchase.services.coupang_service import active_account as _aa
            if _aa() == "new":
                return {"_skip": "카테고리 미해결(0) — 신규계정 필수속성 없음(quota 절약 스킵)"}
        except Exception:
            pass
        # 자동매칭 대상(category="0") 혹은 메타 조회 실패 — 빈 attributes 로 전송(구계정).
        attributes = []

    # 판매 시작/종료
    now = datetime.now(timezone.utc)
    sale_started_at = now.strftime("%Y-%m-%dT%H:%M:%S")
    sale_ended_at = (now + timedelta(days=365 * 5)).strftime("%Y-%m-%dT%H:%M:%S")

    # 이미지(갤러리) — 사용자 정책(2026-06-19): 누끼형 대표 1장만. 아마존 원본 다수 미사용.
    # (제품 이미지들은 아래 상세 contents 에서 노출됨)
    # ── 이미지 정책 분기(2026-07-06): 화장품/건강식품=self_made(누끼) / 그외=amazon(원본) ──
    _pol_g = _image_policy(product_id)
    if _pol_g == "amazon":
        # ★2026-08-08: 대표 1장만. 종전엔 9장을 전부 넣어 상세 contents 와 그대로 중복됐고,
        #   amazon 정책 상품은 그 9장이 대부분 영문 마케팅 그래픽이라 썸네일 품질도 나빴다.
        #   (바로 위 주석의 "누끼형 대표 1장만" 정책을 self_made 분기만 지키고 있었다)
        _raw_g = _raw_amazon_images(product_id, 9) or list(image_urls[:9])
        _rep = _raw_g[0] if _raw_g else None
        images_payload = ([{"imageOrder": 0, "imageType": "REPRESENTATION", "vendorPath": _rep}]
                          if _rep else [])
    else:
        _nuki = []
        try:
            _nuki = select_all_nuki_images(product_id)
        except Exception as _e:
            logger.warning(f"[coupang] 멀티누끼 실패 product={product_id}: {_e}")
            _nuki = []
        if _nuki:
            _rep = _nuki[0]
            images_payload = [{"imageOrder": 0, "imageType": "REPRESENTATION", "vendorPath": _nuki[0]}]
            for _i, _u in enumerate(_nuki[1:], start=1):
                images_payload.append({"imageOrder": _i, "imageType": "DETAIL", "vendorPath": _u})
        else:
            _rep = select_representative_image(product_id)
            if _rep:
                images_payload = [{"imageOrder": 0, "imageType": "REPRESENTATION", "vendorPath": _rep}]
            else:
                images_payload = []
                for i, url in enumerate(image_urls[:9]):
                    images_payload.append({
                        "imageOrder": i,
                        "imageType": "REPRESENTATION" if i == 0 else "DETAIL",
                        "vendorPath": url,
                    })

    # ★갤러리 대표 1장만 (사장 지시 2026-08-14). 기본값 None 이면 종전 동작 그대로다.
    #   usage(모델컷)·spec(도식)이 갤러리에 섞이면 구매자가 제품을 못 알아본다.
    #   상세페이지(contents_payload)는 아래에서 따로 만든다 — 영향 없다.
    if gallery_one and images_payload:
        _rep_only = [x for x in images_payload
                     if x.get("imageType") == "REPRESENTATION"] or images_payload[:1]
        if len(images_payload) > len(_rep_only):
            logger.info("[coupang] 갤러리 %d장 → 대표 1장 (product=%s)",
                        len(images_payload), product_id)
        images_payload = _rep_only[:1]

    # 상세 contents 구성: 상품 이미지(동적) + 세부사항 스펙표(동적) + 정적 정보 배너(전 상품 공통).
    # 쿠팡은 contentsType=HTML에서 inline style 대부분 strip → 이미지 방식으로 통일.
    # 배너 수정은 templates/coupang_banners_src/*.html 편집 후 render_coupang_banners.py 재실행.
    base = PUBLIC_BASE_URL.rstrip("/")
    contents_payload = []

    # altText 용 상품명 — 한글 우선, 없으면 영문. 조회 실패해도 라벨은 나온다.
    try:
        _alt_nm = (p["title_ko"] if "title_ko" in p.keys() else "") or \
                  (p["title_en"] if "title_en" in p.keys() else "") or ""
    except Exception:
        _alt_nm = ""

    def _img_block(content_url):
        return {
            "contentsType": "IMAGE_NO_SPACE",
            "contentDetails": [{"content": content_url, "detailType": "IMAGE",
                                "altText": _alt_text_for(content_url, _alt_nm)}],
        }

    # 상세 contents 순서(2026-06-13): ① 브랜드 정품 배너(최상단) ② 선별 제품컷(플레인 ≤5)
    #   ③ 자체 인포그래픽(색상배너+구성품+특징) ④ 세부사항 스펙표 ⑤ 정보 배너(배송/아마존/필독).
    #   ②③ 은 저작권 방어(자체 제작 콘텐츠 + 실제 제품 사진만 인용) — detail_infographic.py.
    # ★관세 안내 배너(최상단)
    contents_payload.append(_img_block(f"{base}{CUSTOMS_BANNER_PATH}"))
    # ① 브랜드 정품 배너
    contents_payload.append(_img_block(f"{base}{STATIC_BANNER_PATHS[0]}"))
    # ②③ 새 SEO 상세(render_detail: 실물 제품컷 + 에디토리얼 섹션) 우선.
    #     media/products/{pid}/seo_detail.json 매니페스트(URL 배열)가 있으면 그걸 사용,
    #     없으면 기존 선별제품컷 + 자체 인포그래픽으로 폴백.
    from pathlib import Path as _Path
    _md_dir = _Path(__file__).resolve().parent.parent / "media" / "products" / str(product_id)
    _seo_manifest = _md_dir / "seo_detail.json"
    if not _seo_manifest.exists():
        _seo_manifest = _md_dir / "ed_manifest.json"   # ★에디토리얼(B-design) 폴백 — build_detail_contents 와 동일
    # ── 이미지 정책 분기(2026-07-06) ──
    _pol = _image_policy(product_id)
    _used_new_detail = False
    # ★2026-08-08 순서 수정: 자체 제작 상세(에이전트/리치)가 있으면 그것만 쓴다.
    #   종전엔 amazon 정책이면 원본 8장을 먼저 넣고 _used_new_detail=True 로 막으려 했는데,
    #   아래 매니페스트 블록이 그 플래그를 보지 않아 원본+자체제작이 둘 다 붙었다
    #   (실측: 321468 등록 시 원본 8장 + 에이전트 6장 동시 전송).
    #   그룹 경로(build_detail_contents)는 이미 `if not _used` 가드가 있어 정상이었다.
    try:
        if _seo_manifest.exists():
            _urls = json.loads(_seo_manifest.read_text())
            for _u in _urls:
                contents_payload.append(_img_block(_u if str(_u).startswith("http") else f"{base}{_u}"))
            _used_new_detail = bool(_urls)
    except Exception as _e:
        logger.warning(f"[coupang] 새 상세 manifest 실패(폴백) product={product_id}: {_e}")
        _used_new_detail = False
    if not _used_new_detail and _pol == "amazon":
        # 자체 제작 상세가 없을 때만 아마존 원본(구계정 방식)으로 폴백.
        for _ru in _raw_amazon_images(product_id, 8):
            contents_payload.append(_img_block(_ru))
        _used_new_detail = True
    # ★단품 에디토리얼 자동생성 (신규 리스팅도 에디토리얼, 2026-07-05)
    if not _used_new_detail:
        try:
            _sed2 = _ensure_single_editorial(product_id)
            if _sed2:
                for _u2 in _sed2:
                    contents_payload.append(_img_block(_u2 if str(_u2).startswith("http") else f"{base}{_u2}"))
                _used_new_detail = True
        except Exception as _e:
            logger.warning(f"[coupang] 단품 에디토리얼 생성 실패(폴백): {_e}")
    if not _used_new_detail:
        # ② 선별 제품컷 — 분류 실패/빈 결과면 기존 전량(≤5) 폴백
        try:
            from backend.purchase.services.detail_infographic import select_detail_images
            _sel = select_detail_images(product_id, max_n=5)
        except Exception as _e:
            logger.warning(f"[coupang] 이미지 선별 실패(폴백) product={product_id}: {_e}")
            _sel = None
        # ★게이트된 image_urls(_get_product_images: 마케팅제외+색게이트) 우선, 빈 경우만 select 폴백
        #   + 대표이미지와 동일/근접한 컷 제외(갤러리↔상세 중복 방지, 2026-06-30)
        for url in _drop_rep_duplicates(_rep, list(image_urls[:5] or (_sel or []))):
            contents_payload.append(_img_block(url))
        # ③ 자체 인포그래픽 (캐시 — 없으면 1회 생성). 실패해도 리스팅 계속.
        try:
            from backend.purchase.services.detail_infographic import render_infographic
            _ig = render_infographic(product_id)
            if _ig:
                contents_payload.append(_img_block(f"{base}{_ig}"))
        except Exception as _e:
            logger.warning(f"[coupang] 인포그래픽 렌더 실패(계속) product={product_id}: {_e}")
    # ★구성품 가공컷(세트/번들 상품) — components_cut.jpg 존재 시 '구성품' 전용 블록(저작권 안전 자체이미지)
    _cc = _Path(__file__).resolve().parent.parent / "media" / "products" / str(product_id) / "components_cut.jpg"
    if _cc.is_file():
        contents_payload.append(_img_block(f"{base}/api/pa/images/products/{product_id}/components_cut.jpg"))
    _cb = _Path(__file__).resolve().parent.parent / "media" / "products" / str(product_id) / "components_block.jpg"
    if _cb.is_file():
        contents_payload.append(_img_block(f"{base}/api/pa/images/products/{product_id}/components_block.jpg"))
    # ④ 세부사항 스펙표. 2행 미만이면 None → 생략. 렌더 실패가 리스팅을 막지 않도록 방어적 try.
    try:
        from backend.purchase.services.spec_table import render_spec_table
        _spec_url = render_spec_table(product_id)
        if _spec_url:
            contents_payload.append(_img_block(f"{base}{_spec_url}"))
    except Exception as _e:
        logger.warning(f"[coupang] 스펙표 렌더 실패(계속) product={product_id}: {_e}")
    # ⑤ 나머지 정보 배너 (배송/아마존/구매필독) — 브랜드 배너는 ①에서 첨부.
    for rel in STATIC_BANNER_PATHS[1:]:
        contents_payload.append(_img_block(f"{base}{rel}"))

    # 3대 필수정보(2026-08-01 정책) — 브랜드 + 식별번호(GTIN/품번). 구매옵션은 attributes 가 커버.
    #   ★신규계정(2026-05 정책): brandId 필수 → brand_search 로 라이브러리 brandId 해석.
    #     라이브러리에 있으면 brandId+한글명, 없으면 노브랜드(""). force_no_brand=게이팅 폴백.
    _src_brand = (p["brand"] if "brand" in p.keys() else None) or brand
    _brand_id = None
    _uid_required = False
    if _active_acct() == "new":
        if force_no_brand:
            _brand_val = ""
        else:
            _bn = _resolve_brand_new(_src_brand)
            if _bn["brand_id"] and not (_bn["uid_required"] and not _bc):
                _brand_val = _bn["brand_name"] or _src_brand   # 쿠팡 표준 한글 브랜드명
                _brand_id = _bn["brand_id"]
                _uid_required = _bn["uid_required"]
            else:
                # ★brandId 없으면 노브랜드("") — 쿠팡은 brandId 없이 브랜드명만 보내면 거부(2026-07-08 실측 683에러:
                #   "브랜드ID가 필요합니다"). 브랜드명 쓰려면 WING 브랜드관리 수동등록 필요. 라이브러리 매칭만 brand+brandId.
                _brand_val = ""
    else:
        _brand_val = _resolve_brand_ko(_src_brand)
    #   식별번호: GTIN(barcode) 있으면 그걸로 충족, 없으면 modelNo(품번) 채움.
    _model_no = "" if _bc else _resolve_model_no(p["asin"])
    # UID필수 브랜드 → items attributes 에 GTIN 추가(2026 신규 필수속성). _bc 보장(위 조건).
    if _uid_required and _bc:
        attributes = list(attributes or []) + [_gtin_attribute(_bc)]

    # ★상품명 B(좁은 브랜드 음역, 2026-07-01): name이 브랜드로 시작 + 관용영문(BMW 등) 아니면
    #   앞 브랜드를 한글 음역으로 치환(Owala→오왈라). 불일치·약자는 원본 유지(안전).
    name = apply_brand_translit_to_name(name, _src_brand)
    # ★어린이/키즈 완화(2026-07-01): 비-어린이 카테고리면 부수 '어린이'→'온가족용/전연령'(KC 오탐). 완구류는 유지.
    name = soften_kids_terms(name, cat_path)

    # ★ 계정-인식 식별자/코드 — 정적 상수(COUPANG_VENDOR_ID 등)는 import 시점 COUPANG_ACTIVE 로
    #   고정돼 멀티계정(coupang_account("new")) 컨텍스트를 무시하는 버그가 있었음. _vendor()류로 해소.
    from backend.purchase.services.coupang_service import (
        _vendor as _acc_vendor, _user_id as _acc_user,
        _outbound_code as _acc_out, _return_center as _acc_ret)
    _VID = _acc_vendor()
    _UID = _acc_user()
    _OUTBOUND = _acc_out() or COUPANG_OUTBOUND_SHIPPING_PLACE_CODE
    _RETCENTER = _acc_ret() or COUPANG_RETURN_CENTER_CODE

    # ★검색어(seo_tags) 등록시점 보장(2026-07-12): 없으면 로테이션 AI 생성, 4개 전멸이면 등록 보류.
    _seo_lp, _seo_blocked = _ensure_seo_tags(product_id, p["seo_tags"], p["title_ko"] or p["title_en"], cat_path)
    if _seo_blocked:
        return {"ok": False, "skip": True, "error": "AI 소진(GPT+Gemini) — searchTags 생성 불가로 등록 보류"}

    payload = {
        "displayCategoryCode": int(category),
        "sellerProductName": name,
        "displayProductName": name,   # ★노출상품명 명시(그룹 master와 일관, 쿠팡 fallback 모호성 제거)
        "vendorId": _VID,
        "saleStartedAt": sale_started_at,
        "saleEndedAt": sale_ended_at,
        # 브랜드 필수(2026-08-01 정책). 신규계정도 브랜드명 직접입력 통과 확인(2026-06-19,
        # Neutrogena·JUYRLE·Generic 모두 200 SUCCESS) — 과거 '노브랜드 강제'는 해소됨.
        "brand": (_brand_val if _active_acct() == "new" else brand),
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
        "returnCenterCode": _RETCENTER,
        "returnChargeName": P.RETURN_CHARGE_NAME,
        "companyContactNumber": P.RETURN_CONTACT_NUMBER,
        "returnZipCode": P.RETURN_ZIP_CODE,
        "returnAddress": P.RETURN_ADDRESS,
        "returnAddressDetail": P.RETURN_ADDRESS_DETAIL,
        "returnCharge": min(P.COUPANG_RETURN_FEE, max(1000, price // 2 - 500)),
        "outboundShippingPlaceCode": _OUTBOUND,
        "vendorUserId": _UID or _VID,  # WING 로그인 계정 ID (계정-인식)
        "requested": bool(requested),  # True=즉시 승인 요청, False=임시저장 (셀러 후속 승인 필요)
        "items": [{
            "itemName": name[:50],
            "originalPrice": _orig_price,
            "salePrice": price,
            "maximumBuyCount": P.DEFAULT_STOCK,
            "maximumBuyForPerson": 0,
            "outboundShippingTimeDay": 4,           # 2026-06-03 5→4 변경 (운영 정책)
            "maximumBuyForPersonPeriod": 1,
            "unitCount": 1,
            "adultOnly": "EVERYONE",
            "taxType": "TAX",
            "parallelImported": "NOT_PARALLEL_IMPORTED",
            "overseasPurchased": "OVERSEAS_PURCHASED",
            "pccNeeded": True,                       # 통관번호 필수
            "externalVendorSku": f"PA-{product_id}",
            "barcode": _bc,
            "emptyBarcode": not bool(_bc),
            "emptyBarcodeReason": "" if _bc else "COUPANG",
            # 식별번호 정책(2026-08-01): GTIN(barcode) 없으면 modelNo(품번) 필수.
            # SP-API 코드형 model/part 우선, 폴백 ASIN. 상품명 입력은 위반이라 안 씀.
            "modelNo": _model_no,
            "extraProperties": {},
            "certifications": [],
            # 건강식품(해외직구)은 searchTags 에서도 기능성·효능어 제거(식품표시광고법).
            "searchTags": (clean_policy.filter_efficacy_tags(_normalize_search_tags(_seo_lp, brand))
                           if _is_health_food_category(cat_path)
                           else _normalize_search_tags(_seo_lp, brand)),
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
    # ★신규계정 brandId(2026 정책) — 라이브러리 매칭된 경우만 첨부(노브랜드는 미첨부).
    if _brand_id:
        payload["brandId"] = _brand_id
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


def _ensure_title_ko(product_id, title_ko, title_en):
    """등록 직전 한글 제목 보장 — 없거나 영문/garbage면 로테이션 AI로 번역→DB저장→반환.

    ★2026-08-01: 도서(ABIS_BOOK)는 AI 자유번역 대신 SP-API 값으로 조립.
      제목이 고유명사라 번역하면 원제가 소실되고 영문 원제 검색에도 안 잡힌다.
    """
    # ── 도서 분기 (AI 미사용) ──
    try:
        import json as _jb
        from backend.purchase.services.translate_service import is_book_facts, build_book_title
        with get_db() as _c:
            _r = _c.execute("SELECT sp_api_facts_json FROM products WHERE id=?", (product_id,)).fetchone()
        _facts = _jb.loads(_r["sp_api_facts_json"]) if (_r and _r["sp_api_facts_json"]) else {}
        if is_book_facts(_facts):
            _bt = build_book_title(_facts)
            if _bt:
                try:
                    with get_db() as _c:
                        _c.execute("UPDATE products SET title_ko=? WHERE id=?", (_bt, product_id))
                except Exception:
                    pass
                logger.info(f"[title-ko] product {product_id} 도서 조립: {_bt[:60]}")
                return _bt
    except Exception as _e:
        logger.warning(f"[title-ko] 도서 조립 실패 product={product_id}: {_e}")

    try:
        from backend.purchase.services.translate_service import is_title_garbage, translate_ko, clean_ko_title
        import re as _re
        tk = clean_ko_title(title_ko) if title_ko else None
        if tk and not is_title_garbage(tk):
            _kr = len(_re.findall(r"[\uac00-\ud7a3]", tk)); _tot = len(_re.findall(r"[\uac00-\ud7a3A-Za-z]", tk))
            if (not _tot) or (_kr / _tot) >= 0.15:
                return tk  # 이미 한글
        if not title_en:
            return tk or title_ko
        ko = translate_ko(title_en)
        if ko and not is_title_garbage(ko):
            try:
                with get_db() as _c:
                    _c.execute("UPDATE products SET title_ko=? WHERE id=?", (ko, product_id))
            except Exception:
                pass
            return ko
    except Exception:
        pass
    return title_ko


def _ensure_seo_tags(product_id, current_json, title, cat_path):
    """등록 직전 seo_tags 확보 — 없으면 로테이션 AI(GPT+Gemini)로 생성.
    반환 (seo_json, ai_blocked). ai_blocked=True = AI 4개 전멸로 생성불가 → 등록 보류해야."""
    if current_json and str(current_json).strip() not in ("", "[]"):
        return current_json, False
    if not title:
        return current_json, False
    try:
        from backend_shared.ai.service import _call_ai_sync, all_ai_exhausted
        import json as _json, re as _re, os as _os_seo
        cat = (cat_path or "").split(">")[-1].strip()
        prompt = (
            "쿠팡 상품 검색 최적화 태그 생성.\n"
            "상품명: %s\n카테고리: %s\n"
            "규칙: 한국어 검색 키워드 10~15개, 형태소 단위, 효능·과장 표현 제외.\n"
            "JSON 배열만 출력: [\"태그1\",\"태그2\", ...]" % (str(title)[:120], cat)
        )
        # ★검색어는 등록 보류를 좌우하는 핵심단계 — 그룹 컨텍스트의 PA_SKIP_GEMINI(속도용 이미지/카테고리
        #   스킵)를 여기서만 일시 해제해 Gemini 4키까지 전부 시도. GPT+전Gemini 실패해야만 보류(2026-07-24).
        _prev_skip_seo = _os_seo.environ.pop('PA_SKIP_GEMINI', None)
        try:
            res = _call_ai_sync(prompt, max_tokens=400)
        finally:
            if _prev_skip_seo is not None:
                _os_seo.environ['PA_SKIP_GEMINI'] = _prev_skip_seo
        if res:
            m = _re.search(r"\[.*\]", res, _re.S)
            if m:
                tags = _json.loads(m.group(0))
                tags = [str(t).strip() for t in tags if str(t).strip()][:15]
                if tags:
                    js = _json.dumps(tags, ensure_ascii=False)
                    try:
                        with get_db() as _c:
                            _c.execute("UPDATE products SET seo_tags=? WHERE id=?", (js, product_id))
                    except Exception:
                        pass
                    return js, False
        if all_ai_exhausted():
            return current_json, True
    except Exception:
        pass
    return current_json, False


def list_product(product_id: int, image_urls: list[str] | None = None,
                 requested: bool = False, gallery_one: bool = False) -> dict:
    # ★2026-08-08 기본값 True→False (임시저장). 승인요청은 호출측이 명시할 것.
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
               WHERE product_id=? AND channel='coupang' AND coupang_account=?
                 AND COALESCE(status,'') != 'removed'
                 AND COALESCE(coupang_status_name,'') != '상품삭제'""",
            (product_id, _active_acct()),
        ).fetchone()
    if existing and existing["channel_product_id"]:
        return {"ok": False, "skip": True,
                "error": f"이미 등록됨 (channel_product_id={existing['channel_product_id']})"}

    # ── 삭제 이력 검사 (deleted_seller_products) — 재등록 방지 ──
    # ★신 파이프라인 리스크 3축 (2026-08-12) — 그룹 경로와 같은 정책.
    #   기존 게이트들은 ASIN 블랙리스트·키워드라 M12~M14 가 잡는
    #   **미조사 S등급 브랜드**를 못 거른다.
    #   ★판정이 없으면 막지 않는다 — 구 파이프라인 상품엔 import_risk 가 없다.
    try:
        with get_db() as _rc:
            _rr = _rc.execute(
                """SELECT r.axis, r.verdict, r.reason FROM import_risk r
                     JOIN products p ON p.asin = r.asin
                    WHERE p.id=? AND r.verdict IN ('차단','대상','보류','사람검토')
                    LIMIT 1""", (product_id,)).fetchone()
        if _rr:
            _m = "import_risk %s=%s: %s" % (_rr["axis"], _rr["verdict"], (_rr["reason"] or "")[:80])
            logger.warning(f"[upload-coupang] {product_id} 차단 — {_m}")
            return {"ok": False, "skip": True, "error": _m}
    except Exception as _e:  # noqa: BLE001
        logger.warning(f"[upload-coupang] {product_id} import_risk 조회 실패(계속): {str(_e)[:80]}")

    from backend.purchase.services.delete_history import is_previously_deleted
    with get_db() as _c:
        _pa = _c.execute("SELECT asin FROM products WHERE id=?", (product_id,)).fetchone()
    _asin = (_pa["asin"] if _pa else None) or ""
    _blocked, _reason = is_previously_deleted(_asin)
    if _blocked:
        return {"ok": False, "skip": True,
                "error": f"previously_deleted:{_reason}"}

    # ── 중복 ASIN 검사 (clean_policy) ──
    with get_db() as conn:
        asin_row = conn.execute(
            "SELECT asin, amazon_manufacturer, sp_manufacturer FROM products WHERE id=?",
            (product_id,),
        ).fetchone()
    asin = asin_row['asin'] if asin_row else None
    # amazon_manufacturer 는 21.1% 만 채워져 있다 — sp_manufacturer(88.3%) 로 폴백.
    mfr = ((asin_row['amazon_manufacturer'] or '').strip()
           or (asin_row['sp_manufacturer'] or '').strip() or None) if asin_row else None
    if asin:
        is_dup, dup_info = clean_policy.check_duplicate_asin(asin, channel='coupang', exclude_product_id=product_id, coupang_account=_active_acct())
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

    # ★업로드 게이트 (2026-07-23) — 미완성/미번역 상품을 콘텐츠검사·페이로드빌드 전에 보류.
    #   배제검사(이미등록/삭제이력/중복)는 통과한 뒤 실행 → 재시도 무의미한 건 이미 걸러진 상태.
    #   실패=보류(excluded 아님) → 인리치 완료 후 다음 사이클 재등록.
    _gok, _greason = check_upload_ready(product_id)
    if not _gok:
        logger.info(f"[gate] product {product_id} 업로드 보류: {_greason}")
        return {"ok": False, "skip": True, "error": _greason}

    # ── 리콜 차단 게이트 (2026-07-08) — 국표원/CPSC 리콜품 ──
    try:
        from backend.purchase.services.recall_blocklist import is_recalled
        with get_db() as _rc:
            _tr = _rc.execute("SELECT title_en, title_ko FROM products WHERE id=?", (product_id,)).fetchone()
        _rr = is_recalled(asin, (_tr["title_en"] or _tr["title_ko"]) if _tr else None)
        if _rr:
            reason = f"리콜 상품 차단 ({_rr})"
            with get_db() as _rc2:
                _rc2.execute("UPDATE listings_pa SET status='excluded', error_message=?, "
                             "last_synced_at=CURRENT_TIMESTAMP WHERE product_id=? AND channel='coupang'",
                             (reason, product_id))
            try:
                clean_policy.log_violation(stage='upload_coupang', violation_type='recalled',
                    action_taken='excluded', matched_keyword=asin, product_id=product_id,
                    channel='coupang', asin=asin)
            except Exception:
                pass
            return {"ok": False, "skip": True, "error": reason}
    except Exception:
        pass

    # ── 한국 manufacturer 게이트 (IP 라이선스 보호) ──
    kr_blocked, kr_reason = clean_policy.check_korean_manufacturer(mfr)
    if kr_blocked:
        reason = f"한국 manufacturer 차단 ({mfr}) — IP 라이선스 보호 [{kr_reason}]"
        with get_db() as conn:
            conn.execute(
                """UPDATE listings_pa SET status='excluded',
                   error_message=?, last_synced_at=CURRENT_TIMESTAMP
                   WHERE product_id=? AND channel='coupang'""",
                (reason, product_id),
            )
        clean_policy.log_violation(
            stage='upload_coupang', violation_type='korean_manufacturer',
            action_taken='excluded', matched_keyword=mfr,
            product_id=product_id, channel='coupang', asin=asin,
            notes=kr_reason,
        )
        return {"ok": False, "skip": True, "error": reason}

    # 브랜드 블랙리스트 사전 차단 (쿠팡 유통경로 소명 대응 — 정품 민감 브랜드 차단)
    with get_db() as conn:
        prow = conn.execute(
            "SELECT asin, title_en, title_ko, brand, category_path FROM products WHERE id=?",
            (product_id,),
        ).fetchone()
    if prow:
        # ══ 3축 우선 검사 (2026-08-05 사장 지시) ══════════════════════
        #  실제 사고 경로 3가지를 다른 게이트보다 먼저 본다.
        #   축1 리셀금지 브랜드  — Grüns·Loop 유형(브랜드 신고 → 쿠팡 '저작권 침해')
        #   축2 한국 브랜드 역수입 — Spigen·이퀄베리·K-SECRET 유형
        #   축3 KC 어린이제품     — Apitor 유형
        #  + 3축 사유로 삭제된 ASIN 재등록 차단(계정 무관)
        matched = _is_brand_blocked(prow["title_en"] or "", prow["title_ko"] or "", _load_brand_blocklist())
        if not matched:
            try:
                _kb, _kr = clean_policy.check_kc_blocked(
                    prow["title_en"] or "", prow["title_ko"] or "", None, prow["brand"] or "",
                    asin=prow["asin"] or "")
                if _kb:
                    matched = f"KC {_kr}"
            except Exception as _e:
                logger.warning(f"[coupang] 3축 KC 선검사 실패: {_e}")
        # ★삭제이력 ASIN 재등록 차단(2026-08-05) — 계정 무관
        if not matched:
            try:
                _ab, _ar = clean_policy.check_blocked_asin(prow["asin"] or "")
                if _ab:
                    matched = _ar
            except Exception:
                pass

        # ★브랜드필드 차단(2026-08-05): 일반명사 브랜드(Loop·Odyssey·UNO 등)는
        #   제목매칭으로 못 막거나 대량 오차단이 난다. products.brand 정확대조로 보완.
        if not matched:
            try:
                _bfb, _bfr = clean_policy.check_brand_field_blocked(prow["brand"] or "")
                if _bfb:
                    matched = _bfr
            except Exception as _e:
                logger.warning(f"[coupang] 브랜드필드 차단 검사 실패: {_e}")
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

        # ── IP/총판 브랜드 차단 (화장품/건기식 전용, 2026-07-23) ──
        _ip_brand = check_ip_brand_blocked(
            prow["category_path"], prow["title_en"] or "", prow["title_ko"] or "", prow["brand"] or "")
        if _ip_brand:
            reason = f"IP총판브랜드 차단 ({_ip_brand}) — 화장품/건기식 지재권"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='coupang'""",
                    (reason, product_id),
                )
            return {"ok": False, "skip": True, "error": reason}

        # 전기용품 차단 (KC 전기안전인증 — 2026-06-03)
        el_blocked, el_kw = clean_policy.check_electric_appliance(
            prow["title_en"] or "", prow["title_ko"] or "",
        )
        if el_blocked:
            reason = f"전기용품 차단 ({el_kw}) — KC 전기안전인증"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='coupang'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_coupang', violation_type='electric_appliance',
                action_taken='excluded', matched_keyword=el_kw,
                product_id=product_id, channel='coupang', asin=asin,
                notes='KC 전기안전인증 필요',
            )
            return {"ok": False, "skip": True, "error": reason}

        # ── 거울/벽걸이 등 취급제외 카테고리 차단 (액자·가구와 동일 취지, 2026-07-05) ──
        _ec_blocked, _ec_kw = clean_policy.check_excluded_amazon_category(product_id=product_id)
        if _ec_blocked:
            reason = f"취급제외 카테고리 차단 ({_ec_kw}) — 거울/벽걸이(파손·대형 리스크)"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='coupang'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_coupang', violation_type='excluded_category',
                action_taken='excluded', matched_keyword=_ec_kw,
                product_id=product_id, channel='coupang', asin=asin,
                notes='거울/벽걸이 취급제외',
            )
            return {"ok": False, "skip": True, "error": reason}

        # ── 하드케이스 홀드(2026-07-05): 깨끗한 제품사진 0장 + AI 클린컷(themed/design_cut) 없음
        #   → rembg/마케팅 대체 금지(저작권 위험). Gemini 회복 후 design_cut 생성 시 재시도. ──
        try:
            from backend.purchase.services.design_cut import is_zero_clean as _izc_hold
            from pathlib import Path as _P_hold
            _mdh = _P_hold(__file__).resolve().parent.parent / 'media' / 'products' / str(product_id)
            if _image_policy(product_id) == 'self_made' and _izc_hold(product_id) and not any((_mdh / _f).is_file() for _f in ('themed_cut.jpg', 'design_cut.jpg')):
                return {"ok": False, "skip": True, "error": "clean 이미지 없음(AI 클린컷 대기) — 리스팅 보류"}
        except Exception:
            pass

        # ── 목록통관 면세 한도 차단 (원가 $150 초과 = 관세 발생, 구매대행 부적합) ──
        with get_db() as conn:
            _cr = conn.execute("SELECT cost_usd FROM products WHERE id=?", (product_id,)).fetchone()
        if _exceeds_customs_limit(_cr["cost_usd"] if _cr else None):
            _cu = float(_cr["cost_usd"])
            reason = f"관세 한도 초과 차단 (원가 ${_cu:.2f} > ${int(CUSTOMS_DUTY_FREE_USD)}) — 목록통관 면세한도 초과로 관세 발생"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='coupang'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_coupang', violation_type='customs_over_limit',
                action_taken='excluded', matched_keyword=f"${_cu:.2f}",
                product_id=product_id, channel='coupang', asin=asin,
                notes='목록통관 면세한도($150) 초과',
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

        # ── 도수/광학 보정 의료기기 차단 (의료기기법, 2026-05-31 적발건 — 다초점 마스크) ──
        op_blocked, op_reason = clean_policy.check_optical_medical_device(
            prow["title_en"], prow["title_ko"],
        )
        if op_blocked:
            reason = f"광학 의료기기 차단 ({op_reason}) — 의료기기법 분류 가능"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='coupang'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_coupang', violation_type='optical_medical_device',
                action_taken='excluded', matched_keyword=op_reason,
                product_id=product_id, channel='coupang',
                original_text=prow['title_en'],
            )
            return {"ok": False, "skip": True, "error": reason}

        # ── DTC 유전자검사 키트 영구 차단 (생명윤리법 제49조1항, 2026-05-30 적발건) ──
        gk_blocked, gk_kw = clean_policy.check_prohibited_genetic_kit(
            prow["title_en"], prow["title_ko"]
        )
        if gk_blocked:
            reason = f"DTC 유전자검사 키트 차단 ({gk_kw}) — 생명윤리법 제49조1항 위반"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='coupang'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_coupang', violation_type='dtc_genetic_kit',
                action_taken='excluded', matched_keyword=gk_kw,
                product_id=product_id, channel='coupang',
                original_text=prow['title_en'],
            )
            return {"ok": False, "skip": True, "error": reason}

        # ── 의약외품 차단 (약사법 — 2026-06-13 탐폰 적발) ──
        qd_blocked, qd_kw = clean_policy.check_quasi_drug(prow["title_ko"], prow["title_en"])
        if qd_blocked:
            reason = f"의약외품 차단 ({qd_kw}) — 약사법 무허가 의약외품"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='coupang'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_coupang', violation_type='quasi_drug',
                action_taken='excluded', matched_keyword=qd_kw,
                product_id=product_id, channel='coupang',
                original_text=prow['title_ko'],
            )
            return {"ok": False, "skip": True, "error": reason}

        # ── 의류·신발 임시 차단 (PA_DISABLE_APPAREL_SHOES_BLOCK=1 로 해제) ──
        # 2026-07-18 강화: title_en + 카테고리 경로 (아마존 + 쿠팡) 동시 검사
        _ap_cat = prow.get("category_path", "") or "" if hasattr(prow, "get") else (prow["category_path"] or "")
        _ap_cup_code = existing["coupang_category_code"] if existing else None
        if _ap_cup_code:
            try:
                with get_db() as _apc:
                    _apr = _apc.execute(
                        "SELECT path FROM coupang_categories WHERE code=? LIMIT 1", (_ap_cup_code,)
                    ).fetchone()
                    if _apr and _apr["path"]:
                        _ap_cat = (_ap_cat + " | " + _apr["path"]).strip(" |")
            except Exception:
                pass
        ap_blocked, ap_kw = clean_policy.check_blocked_apparel_shoes(
            prow["title_ko"], prow["title_en"], _ap_cat,
        )
        if ap_blocked:
            reason = f"의류·신발 임시 차단 ({ap_kw}) — 사장님 별도 지시 전까지"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='coupang'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_coupang', violation_type='apparel_shoes_blocked',
                action_taken='excluded', matched_keyword=ap_kw,
                product_id=product_id, channel='coupang',
                original_text=prow['title_ko'],
            )
            return {"ok": False, "skip": True, "error": reason}

        # ── KC 비면제 품목 차단 (KC마크 없이 구매대행 불가) ──
        kc_blocked, kc_reason = clean_policy.check_kc_blocked(
            prow["title_en"] or "", prow["title_ko"] or "",
            coupang_category_code=(existing["coupang_category_code"] if existing else None),
            brand=(prow["brand"] or ""),
            asin=prow["asin"] or "",
        )
        if kc_blocked:
            reason = f"KC 비면제 품목 차단 ({kc_reason}) — KC마크 없이 구매대행 불가"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='coupang'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_coupang', violation_type='kc_required',
                action_taken='excluded', matched_keyword=kc_reason,
                product_id=product_id, channel='coupang', asin=asin,
                original_text=prow['title_en'],
            )
            return {"ok": False, "skip": True, "error": reason}

    # ── 마진 게이트 — net_margin_krw < 15,000원 차단 (사용자 정책)
    # STALE (sale_krw/cost 미책정) 은 통과 (사후 monitor 가 잡음).
    try:
        from backend.purchase.services.margin_gate import block_listing_if_low_margin
        blocked, mreason = block_listing_if_low_margin(product_id, channel="coupang")
        if blocked:
            return {"ok": False, "skip": True, "error": f"마진 차단: {mreason}"}
    except Exception as e:
        logger.warning(f"[coupang] product {product_id} margin gate 예외(통과): {e}")

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

    payload = build_payload(product_id, image_urls=image_urls, requested=requested,
                            gallery_one=gallery_one)
    if not payload:
        # listings_pa 정합성 — pending 채로 두지 말고 명시 마킹
        with get_db() as conn:
            conn.execute(
                """UPDATE listings_pa SET status='excluded',
                   error_message=?, last_synced_at=CURRENT_TIMESTAMP
                   WHERE product_id=? AND channel='coupang'""",
                (f"페이로드 생성 실패 (이미지 부족/검증 실패)", product_id),
            )
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
    # ★신규계정: brandId 등록이 게이팅(정품 인증요구) 등으로 4xx 실패하면 노브랜드로 1회 재시도.
    if (_active_acct() == "new" and payload.get("brandId")
            and (not result or (isinstance(result, dict) and result.get("_error")))):
        logger.warning(f"[coupang] product {product_id} brandId={payload.get('brandId')} 등록실패 → 노브랜드 폴백")
        payload_nb = build_payload(product_id, image_urls=image_urls, requested=requested,
                                   force_no_brand=True, gallery_one=gallery_one)
        if payload_nb and not (isinstance(payload_nb, dict) and payload_nb.get("_skip")
                               and "displayCategoryCode" not in payload_nb):
            payload = payload_nb
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
    try:
        from backend.purchase.services.coupang_service import active_account as _aa
        _acct = _aa()
    except Exception:
        _acct = _active_acct()
    with get_db() as conn:
        # ★write-back upsert(2026-07-06): 기존 행 없으면 INSERT(중복게이트가 읽을 수 있게). UNIQUE(product_id,channel).
        # ★2026-08-03: approval_requested_at 을 requested 와 무관하게 무조건 기록하고 있었다.
        #   임시저장(requested=False)으로 올려도 "승인요청됨"으로 남아, coupang_approval 의
        #   일괄 승인요청 대상(approval_requested_at IS NULL)에서 빠져 영영 요청이 안 나갔다.
        #   (실측: 2026-08-03 등록분 312건이 이 상태) → 실제 요청한 경우에만 기록한다.
        _appr = "CURRENT_TIMESTAMP" if requested else "NULL"
        conn.execute(
            f"""INSERT INTO listings_pa (product_id, channel, channel_product_id, coupang_account,
                 status, approval_requested_at, last_synced_at, acct_key)
               VALUES (?, 'coupang', ?, ?, 'listed', {_appr}, CURRENT_TIMESTAMP, ?)
               ON CONFLICT(product_id, channel, acct_key) DO UPDATE SET
                 channel_product_id=excluded.channel_product_id, status='listed',
                 approval_requested_at={_appr}, coupang_account=excluded.coupang_account,
                 last_synced_at=CURRENT_TIMESTAMP, error_message=NULL""",
            (product_id, seller_product_id, _acct, _acct or ""),
        )
        _sync_product_status(conn, product_id)
    return {"ok": True, "result": result}
