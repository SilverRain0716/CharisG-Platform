"""
coupang_attributes.py — 쿠팡 카테고리별 MANDATORY 속성 자동 주입 (v3 - Tier 2 AI).

**정책 (2026-04-24 Tier 2 추가)**:
  - **Tier 2 (우선)**: Gemini 에 쿠팡 카테고리 메타(inputValues/usableUnits) + 상품 제목/설명
    을 전달해 한번에 모든 MANDATORY 속성값을 추출. 반환값을 메타 규격(SELECT allowedValues,
    NUMBER 단위)에 대해 validation 후 채택.
  - **Tier 1 (fallback)**: AI 실패 시 기존 정규식/사전 기반 추출.
  - 여전히 실패하면 skip (excluded).
  - oz/온스 → ml 변환 (쿠팡은 usableUnits 외 단위 거부). 20oz → 591ml.
  - 제목은 손대지 않음 — 중복 단어 경고는 수용 (데이터 정확도 우선).

**반환**:
  build_required_attributes(meta, product) -> tuple[list[dict], str]
      (attributes, skip_reason)
      skip_reason이 있으면 상품 전체를 skip 처리.
"""
import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


# ── 색상 추출 ─────────────────────────────
_KO_COLORS = (
    "블랙", "화이트", "레드", "블루", "그린", "옐로우", "핑크", "퍼플",
    "브라운", "그레이", "실버", "골드", "베이지", "네이비", "민트", "카키",
    "아이보리", "오렌지", "크림", "버건디",
)
_EN_COLOR_MAP = {
    "black": "블랙", "white": "화이트", "red": "레드", "blue": "블루",
    "green": "그린", "yellow": "옐로우", "pink": "핑크", "purple": "퍼플",
    "brown": "브라운", "gray": "그레이", "grey": "그레이", "silver": "실버",
    "gold": "골드", "beige": "베이지", "navy": "네이비", "orange": "오렌지",
}
_EN_COLOR_RE = re.compile(
    r"\b(" + "|".join(_EN_COLOR_MAP.keys()) + r")\b", flags=re.IGNORECASE
)


def _extract_color(title: str) -> Optional[str]:
    if not title:
        return None
    # 한글 색상 단어 우선
    for c in _KO_COLORS:
        if c in title:
            return c
    m = _EN_COLOR_RE.search(title)
    if m:
        return _EN_COLOR_MAP[m.group(1).lower()]
    return None


# ── 수량 추출 ─────────────────────────────
# 우선순위: 개입 > 정/캡슐 > 개 > 팩 > 세트 > pack/pk/pcs/tablets/capsules
_QTY_UNITS_KO = ("개입", "정", "캡슐", "타블렛", "포드", "개", "팩", "세트", "켤레", "짝")
_QTY_UNITS_EN_TO_BASIC = {
    "tablets": "정", "tablet": "정",
    "capsules": "캡슐", "capsule": "캡슐",
    "softgels": "캡슐", "softgel": "캡슐",
    "gummies": "정", "gummy": "정",
    "pods": "개", "pod": "개",
    "count": "개", "ct": "개",
}


def _extract_quantity(title: str, basic_unit: str) -> Optional[str]:
    if not title:
        return None
    # 한글 단위 (우선순위 순서)
    for unit in _QTY_UNITS_KO:
        m = re.search(rf"(\d+)\s*{unit}(?![\w가-힣])", title)
        if m:
            return f"{m.group(1)}{unit}"
    # 영문 단위 → basic_unit 대용 한글
    for en, ko in _QTY_UNITS_EN_TO_BASIC.items():
        m = re.search(rf"(\d+)\s*{en}\b", title, flags=re.IGNORECASE)
        if m:
            unit_use = ko if (basic_unit in ("개", "정", "캡슐") or not basic_unit) else basic_unit
            return f"{m.group(1)}{unit_use}"
    # 일반 영문 단위 (basic_unit 로 통일)
    m = re.search(r"(\d+)\s*(?:pack|pk|pcs|set|packs)(?![\w가-힣])", title, flags=re.IGNORECASE)
    if m:
        return f"{m.group(1)}{basic_unit or '개'}"
    return None


# ── 용량 추출 (US 단위→ml 변환) ──────────────
# 1 fl oz = 29.5735 ml, 1 gallon = 3785.41 ml, 1 quart = 946.353 ml, 1 pint = 473.176 ml
def _format_volume(ml: float, usable_units: tuple) -> str:
    if "ml" in usable_units:
        return f"{int(round(ml))}ml"
    if "L" in usable_units:
        return f"{ml/1000:g}L"
    return f"{int(round(ml))}ml"


def _extract_volume(title: str, usable_units: tuple) -> Optional[str]:
    if not title:
        return None
    # gallon / 갤런 (가장 큰 단위 우선)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:gallons?|gal|갤런)\b", title, flags=re.IGNORECASE)
    if m:
        return _format_volume(float(m.group(1)) * 3785.41, usable_units)
    # quart
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:quarts?|qt|쿼트)\b", title, flags=re.IGNORECASE)
    if m:
        return _format_volume(float(m.group(1)) * 946.353, usable_units)
    # pint
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:pints?|pt|파인트)\b", title, flags=re.IGNORECASE)
    if m:
        return _format_volume(float(m.group(1)) * 473.176, usable_units)
    # fluid ounce (fl oz / fl. oz / fluid ounce)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:fl[\s.]*oz|fluid[\s_]?ounces?)\b", title, flags=re.IGNORECASE)
    if m:
        return _format_volume(float(m.group(1)) * 29.5735, usable_units)
    # 일반 oz / 온스 (fl oz 가 위에서 매칭됐으면 안 옴)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:oz|온스)\b", title, flags=re.IGNORECASE)
    if m:
        return _format_volume(float(m.group(1)) * 29.5735, usable_units)
    # ml 직접
    m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", title, flags=re.IGNORECASE)
    if m:
        return _format_volume(float(m.group(1)), usable_units)
    # L 직접 (단위 단어 경계 정확히)
    m = re.search(r"(\d+(?:\.\d+)?)\s*L(?![\w가-힣])", title)
    if m:
        return _format_volume(float(m.group(1)) * 1000, usable_units)
    return None


# ── 중량 추출 ─────────────────────────────
def _extract_weight(title: str, usable_units: tuple) -> Optional[str]:
    if not title:
        return None
    # lb / lbs / 파운드 → g 변환 (1 lb = 453.592 g)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:lbs?|파운드)", title, flags=re.IGNORECASE)
    if m:
        g = round(float(m.group(1)) * 453.592)
        if "kg" in usable_units and g >= 1000:
            return f"{g/1000:g}kg"
        if "g" in usable_units:
            return f"{g}g"
        return f"{g}g"
    # kg 직접
    m = re.search(r"(\d+(?:\.\d+)?)\s*kg", title, flags=re.IGNORECASE)
    if m:
        val = float(m.group(1))
        if "kg" in usable_units:
            return f"{val:g}kg"
        if "g" in usable_units:
            return f"{int(val * 1000)}g"
    # g 직접
    m = re.search(r"(\d+(?:\.\d+)?)\s*g(?![\w가-힣])", title, flags=re.IGNORECASE)
    if m:
        val = float(m.group(1))
        if "g" in usable_units:
            return f"{int(val) if val == int(val) else val}g"
        if "kg" in usable_units:
            return f"{val/1000:g}kg"
    return None


# ── 프리사이즈 허용 카테고리 판정 ────────────────
# path의 루트/일부가 아래 prefix로 시작하면 사이즈 추출 실패 시
# "프리사이즈" soft fallback 허용. 해당 카테고리의 제품은 가변/조절형이
# 많아 고정 치수 없이도 주문 정확도에 영향이 적다.
_SOFT_SIZE_PATH_PREFIXES = (
    "스포츠/레져",           # 헬스/요가/캠핑 등 운동기구·도구
    "반려/애완용품",          # 동물 맞춤 제품
    "패션의류잡화",           # 옷/모자/스카프
    "뷰티",                 # 소형 미용 아이템
    "주방용품",              # 컵/칼/도구 (용량은 별도로 strict)
    "완구/취미",             # 장난감
    "식품",                 # 사이즈 무관
    "생활용품 > 욕실용품",     # 수건/타월
    "생활용품 > 세제",         # 세제류
)


def _is_soft_size_path(cat_path: str) -> bool:
    if not cat_path:
        return False
    for prefix in _SOFT_SIZE_PATH_PREFIXES:
        if cat_path.startswith(prefix):
            return True
    return False


# ── 사이즈/길이 추출 ───────────────────────
_SIZE_FASHION_TOKENS = ("XXL", "XL", "XS", "L", "M", "S")


def _extract_size(title: str) -> Optional[str]:
    """일반 사이즈 — 인치/cm/mm 등 치수 우선. XL/M은 패션 전용."""
    if not title:
        return None
    # 복합 치수 (3.1인치x2.4인치, 30x40cm 등)
    m = re.search(
        r"(\d+(?:\.\d+)?(?:\s*[xX×]\s*\d+(?:\.\d+)?)+\s*(?:인치|inch|cm|mm))",
        title, flags=re.IGNORECASE,
    )
    if m:
        return re.sub(r"\s+", "", m.group(1)).replace("inch", "인치")
    # 단일 치수
    m = re.search(r"(\d+(?:\.\d+)?\s*(?:인치|inch|cm|mm))", title, flags=re.IGNORECASE)
    if m:
        return re.sub(r"\s+", "", m.group(1)).replace("inch", "인치")
    return None


def _extract_length(title: str, basic_unit: str) -> Optional[str]:
    """길이 전용. cm/mm/m 및 인치 (→cm 변환) 지원.

    복합 치수("48x24인치")에서도 첫 숫자만 가로길이로 추출.
    """
    if not title:
        return None
    # cm/mm/m 직접 매칭 (단위 유지)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(cm|mm|m)(?![\w가-힣])", title, flags=re.IGNORECASE)
    if m:
        unit = m.group(2).lower()
        return f"{m.group(1)}{unit}"
    # 인치 → cm 변환 (1 인치 = 2.54 cm). basicUnit이 cm일 때만 변환.
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:인치|inch)", title, flags=re.IGNORECASE)
    if m and (basic_unit or "cm") in ("cm", "mm"):
        inches = float(m.group(1))
        cm = round(inches * 2.54, 1)
        if basic_unit == "mm":
            return f"{int(cm * 10)}mm"
        return f"{cm:g}cm"
    return None


# ── Tier 2: Gemini 기반 속성 일괄 추출 ────────────
def _ai_extract_mandatory_values(mandatory_attrs: list[dict], product: dict) -> dict:
    """Gemini 에 상품 정보 + 쿠팡 속성 스펙을 전달해 {속성명: 값} 추출.

    실패/파싱불가 시 빈 dict. 부분 실패 시 해당 속성만 제외된 dict.
    """
    title = f"{product.get('title_ko') or ''} {product.get('title_en') or ''}".strip()
    desc = (product.get('description_ko') or product.get('description_en') or '')[:500]
    if not title:
        return {}

    # 속성 스펙 구조화 — SELECT 는 allowedValues, INPUT 은 단위 정보
    specs = []
    for a in mandatory_attrs:
        spec = {
            "name": (a.get("attributeTypeName") or "").strip(),
            "dataType": a.get("dataType") or "STRING",
            "inputType": a.get("inputType") or "INPUT",
        }
        if spec["inputType"] == "SELECT":
            vals = []
            for v in (a.get("inputValues") or []):
                if isinstance(v, dict):
                    vals.append(v.get("attributeValueName"))
                else:
                    vals.append(str(v))
            spec["allowedValues"] = [v for v in vals if v]
        else:
            spec["basicUnit"] = a.get("basicUnit") or ""
            spec["usableUnits"] = list(a.get("usableUnits") or [])
        specs.append(spec)

    prompt = (
        "당신은 쿠팡 상품 등록 전문가입니다. 상품 정보를 보고 각 필수 속성의 값을 "
        "쿠팡 규격에 맞게 추출하세요.\n\n"
        f"상품 제목: {title[:300]}\n"
        f"상품 설명: {desc}\n\n"
        "필수 속성 스펙 (JSON):\n"
        f"{json.dumps(specs, ensure_ascii=False)}\n\n"
        "규칙:\n"
        "- SELECT 타입: allowedValues 중 하나만 정확히 선택 (복사).\n"
        "- NUMBER + INPUT 타입: 숫자+단위 형식. 단위는 usableUnits 내 값 (예: '591ml', '3개').\n"
        "- STRING + INPUT 타입: 30자 이내 간결한 텍스트.\n"
        "- 상품 정보에서 추출 불가능하면 null.\n\n"
        "응답은 JSON만, 다른 텍스트 없이:\n"
        "{\"속성명1\": \"값1\", \"속성명2\": null, ...}\n"
    )

    try:
        from backend_shared.ai.service import _call_gemini
        result = _call_gemini(prompt, max_tokens=800)
    except Exception as e:
        logger.warning(f"Tier 2 AI 호출 예외: {e}")
        return {}
    if not result:
        return {}

    # JSON 추출 (앞뒤 설명 텍스트 방어)
    m = re.search(r'\{[\s\S]*\}', result)
    if not m:
        return {}
    try:
        parsed = json.loads(m.group(0))
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    # null/빈 문자열 제외
    return {k: v for k, v in parsed.items() if isinstance(v, str) and v.strip()}


def _validate_ai_value(attr_meta: dict, value: str) -> bool:
    """AI 추출값이 쿠팡 메타 규격에 맞는지 검증."""
    if not isinstance(value, str) or not value.strip():
        return False
    value = value.strip()
    input_type = attr_meta.get("inputType") or "INPUT"
    data_type = attr_meta.get("dataType") or "STRING"

    if input_type == "SELECT":
        allowed = []
        for v in (attr_meta.get("inputValues") or []):
            if isinstance(v, dict):
                allowed.append(v.get("attributeValueName"))
            else:
                allowed.append(str(v))
        return value in [a for a in allowed if a]

    # INPUT 타입
    if data_type == "NUMBER":
        m = re.match(r'^(\d+(?:\.\d+)?)\s*(.+)$', value)
        if not m:
            return False
        unit = m.group(2).strip()
        usable = [u for u in (attr_meta.get("usableUnits") or [])]
        basic = attr_meta.get("basicUnit") or ""
        if unit in usable or unit == basic:
            return True
        return False

    # INPUT STRING — 자유 텍스트, 길이만 체크
    return len(value) <= 50


def _describe_validation_failure(attr_meta: dict, value: str) -> str:
    """_validate_ai_value 가 False 반환했을 때 사유 문자열."""
    if not isinstance(value, str) or not value.strip():
        return "값 비어있음"
    value = value.strip()
    input_type = attr_meta.get("inputType") or "INPUT"
    data_type = attr_meta.get("dataType") or "STRING"

    if input_type == "SELECT":
        allowed = []
        for v in (attr_meta.get("inputValues") or []):
            if isinstance(v, dict):
                allowed.append(v.get("attributeValueName"))
            else:
                allowed.append(str(v))
        allowed = [a for a in allowed if a]
        sample = allowed[:8]
        return f"SELECT allowedValues 불일치 (허용 샘플 {sample}{'...' if len(allowed) > 8 else ''})"

    if data_type == "NUMBER":
        m = re.match(r'^(\d+(?:\.\d+)?)\s*(.+)$', value)
        if not m:
            return "NUMBER 패턴 불일치 (예: '500ml' 형식 필요)"
        unit = m.group(2).strip()
        usable = list(attr_meta.get("usableUnits") or [])
        basic = attr_meta.get("basicUnit") or ""
        return f"단위 '{unit}' 허용 안됨 (basic='{basic}', usable={usable})"

    if len(value) > 50:
        return f"STRING 길이 초과 ({len(value)}>50)"
    return "알 수 없는 사유"


# ── 핵심 빌더 (Tier 2 AI → Tier 1 정규식 fallback) ────────────
def _load_saved_coupang_attrs(product: Optional[dict]) -> dict:
    """products.coupang_attributes_json 에서 수동/엄격추출 저장값 로드.

    포맷: {속성명: 값} — v24부터 채널 전용 컬럼 분리, coupang_attrs 래핑 키 제거.
    """
    if not product:
        return {}
    raw = product.get("coupang_attributes_json")
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def build_required_attributes(
    meta: Optional[dict], product: Optional[dict] = None, cat_path: str = "",
) -> tuple[list[dict], str]:
    """MANDATORY 속성 리스트 + skip 사유 반환.

    흐름: Tier 0 (저장된 수동/엄격추출 값) → Tier 2 (AI 일괄 추출) → Tier 1 (정규식) fallback.
    Tier 0 이 속성을 채우면 Tier 2 AI 호출을 스킵해 토큰 절감.
    """
    if not meta:
        return [], ""

    # 영양제 카테고리(식품>건강식품) — 셀러 자동생성옵션 활성화됨.
    # mandatory attribute 4개 보내면 "옵션 4개 한도 초과" 거부 → 빈 배열로 보내 쿠팡이 자동 생성.
    if cat_path and (cat_path.startswith("식품 > 건강식품") or cat_path.startswith("식품>건강식품")):
        logger.info(f"[coupang attrs] 영양제 카테고리 — attributes 비움 (자동생성옵션)")
        return [], ""

    attrs_meta = meta.get("attributes") or []
    mandatory = [a for a in attrs_meta if a.get("required") == "MANDATORY"]
    if not mandatory:
        return [], ""

    # Tier 0: 저장된 값 먼저 로드 (수동 입력 / 엄격 재추출 결과)
    saved_values = _load_saved_coupang_attrs(product)

    # Tier 2: AI 일괄 추출 — 저장값으로 이미 커버되는 속성은 요청에서 제외
    # COUPANG_DISABLE_AI=1 이면 Tier 2 스킵 (Gemini 일일 한도 초과 시 긴급 우회)
    import os as _os
    _skip_ai = _os.environ.get("COUPANG_DISABLE_AI", "").strip() in ("1", "true", "yes")
    ai_target = [a for a in mandatory
                 if (a.get("attributeTypeName") or "").strip() not in saved_values]
    if _skip_ai:
        ai_values = {}
    else:
        ai_values = _ai_extract_mandatory_values(ai_target, product) if (product and ai_target) else {}

    title = ""
    if product:
        title = f"{product.get('title_ko') or ''} {product.get('title_en') or ''}".strip()
    soft_size = _is_soft_size_path(cat_path)

    result = []
    saved_hits = 0
    ai_hits = 0
    tier1_hits = 0
    for a in mandatory:
        name = (a.get("attributeTypeName") or "").strip()
        if not name:
            continue
        data_type = a.get("dataType") or "STRING"
        input_type = a.get("inputType") or "INPUT"
        input_values = a.get("inputValues") or []
        basic_unit = a.get("basicUnit") or ""
        usable_units = tuple(a.get("usableUnits") or [])

        value = None

        # 0차: 저장된 수동/엄격추출 값 + validation
        saved_candidate = saved_values.get(name)
        if saved_candidate and _validate_ai_value(a, saved_candidate):
            value = saved_candidate.strip()
            saved_hits += 1

        # 1차: AI 결과 + validation
        if value is None:
            ai_candidate = ai_values.get(name)
            if ai_candidate and _validate_ai_value(a, ai_candidate):
                value = ai_candidate.strip()
                ai_hits += 1

        # 2차: Tier 1 fallback (정규식/SELECT 첫 값 등)
        if value is None:
            value = _resolve_attribute_value(
                name=name,
                data_type=data_type,
                input_type=input_type,
                input_values=input_values,
                basic_unit=basic_unit,
                usable_units=usable_units,
                title=title,
                soft_size=soft_size,
            )
            if value is not None:
                tier1_hits += 1

        if value is None:
            return [], f"MANDATORY '{name}' 정보 부족 (Tier 2+Tier 1 모두 실패)"

        result.append({
            "attributeTypeName": name,
            "attributeValueName": value,
            "exposed": a.get("exposed", "EXPOSED"),
            "editable": True,
        })

    if result:
        logger.info(
            f"[coupang attrs] MANDATORY {len(result)}개: "
            f"Saved {saved_hits} / AI {ai_hits} / Tier1 {tier1_hits}"
        )
    return result, ""


def _resolve_attribute_value(
    *, name: str, data_type: str, input_type: str,
    input_values: list, basic_unit: str, usable_units: tuple, title: str,
    soft_size: bool = False,
) -> Optional[str]:
    """하나의 MANDATORY 속성에 대해 값을 결정. None 반환 시 skip 대상."""
    # 1) SELECT(ENUM): 첫 번째 허용값
    if input_type == "SELECT" and input_values:
        v0 = input_values[0]
        v = v0.get("attributeValueName") if isinstance(v0, dict) else str(v0)
        if v:
            return v

    # 이하는 INPUT 타입 처리
    # 2) STRING — 종류별 추출/기본값 (Tier 1 soft fallback 정책으로 skip 최소화)
    if data_type == "STRING":
        # 색상: 추출 실패 시 "혼합색상" soft fallback
        if "색상" in name or "컬러" in name or "칼라" in name:
            return _extract_color(title) or "혼합색상"
        # 패션 사이즈: 이미 path가 "패션의류잡화" prefix인 경우 soft_size=True → 프리사이즈 OK
        if "패션" in name and "사이즈" in name:
            return _extract_size(title) or "프리사이즈"
        # 신발 사이즈: 추출 필수 (숫자), 실패 시 soft "프리사이즈"
        if "신발" in name and "사이즈" in name:
            return _extract_size(title) or "프리사이즈"
        # 일반 사이즈/커버사이즈/치수: path 기반 soft/strict 분기
        if "사이즈" in name or "치수" in name:
            extracted = _extract_size(title)
            if extracted:
                return extracted
            if soft_size:
                return "프리사이즈"
            # strict 카테고리라도 Tier 1 soft fallback — 추후 제거 가능
            return "상세설명참조"
        # 구성품: 모호하므로 기본값 OK
        if "구성품" in name:
            return "기본 구성"
        # 모델명/품번: soft fallback
        if "모델명" in name or "품번" in name:
            return "상세설명참조"
        # 도서(저자/출판사/ISBN): 구매대행 상품은 해외 원서 → soft fallback
        if name == "저자":
            return "해외출판사"
        if name == "출판사":
            return "해외출판사"
        if name == "ISBN":
            return "없음"
        # 기타 STRING: 범용 soft fallback (쿠팡이 수용하지 않으면 추후 케이스별 대응)
        return "상세설명참조"

    # 3) NUMBER — 종류별 추출 (soft fallback 있음 / 없음 분기)
    if data_type == "NUMBER":
        # 수량: soft fallback "1개" 허용 (대부분 1개가 기본)
        if "수량" in name or "개수" in name or "입수" in name:
            v = _extract_quantity(title, basic_unit)
            if v:
                return v
            # fallback: basic_unit이 유효하면 "1<unit>"
            if basic_unit:
                return f"1{basic_unit}"
            return "1개"
        # 캡슐/정 (보충제·식기세척기 정제 등): _extract_quantity 활용
        if "캡슐" in name or "정" in name or "타블렛" in name or "포드" in name:
            v = _extract_quantity(title, basic_unit)
            if v:
                return v
            # 사용 가능 단위가 ['정','회분'] 같이 정해져 있으면 그 중 첫 번째 + "1"
            if usable_units:
                return f"1{usable_units[0]}"
            return None
        # 용량: strict — 추출 실패 시 skip
        if "용량" in name or "부피" in name or "개당 용량" in name:
            return _extract_volume(title, usable_units)  # None이면 skip
        # 중량: strict
        if "중량" in name or "무게" in name or "개당 중량" in name:
            return _extract_weight(title, usable_units)
        # 길이류 (가로길이/세로길이/높이/사이즈 등): strict
        # "최대커버사이즈"(프라이팬 지름) 등 NUMBER+사이즈 케이스 포함.
        if (
            "길이" in name or "높이" in name or "폭" in name or "너비" in name
            or "사이즈" in name or "지름" in name or "두께" in name
        ):
            return _extract_length(title, basic_unit)
        # 기타 NUMBER: skip
        return None

    return None


# ── 엄격 재추출 (SP-API + 강화 Gemini) ─────────────────────────

def _fetch_sp_api_facts(asin: str) -> dict:
    """SP-API facts — sp_api_facts 단일 호출 + DB 캐시 경유.

    반환은 정규화된 dict (sp_api_facts.normalize_catalog_item 참조). Gemini 프롬프트에
    json.dumps 로 dump 하면 키 이름이 명시적이라 AI 가 더 잘 해석한다.

    실패/ASIN 없음 시 빈 dict.
    """
    if not asin:
        return {}
    try:
        from backend.purchase.services.sp_api_facts import get_strict_facts
    except ImportError:
        logger.warning("[strict] sp_api_facts 모듈 없음")
        return {}
    try:
        return get_strict_facts(asin)
    except Exception as e:
        logger.warning(f"[strict] SP-API facts 조회 실패 {asin}: {e}")
        return {}


_STRICT_PROMPT_TMPL = (
    "당신은 쿠팡 상품 등록 전문가입니다. 아마존 원본 데이터에서 쿠팡 MANDATORY 속성값을 "
    "엄격히 추출하세요. 필요 시 미국 단위를 한국 단위로 변환하고, 서빙/팩 정보로 총량을 계산하세요.\n\n"
    "== 상품 정보 ==\n"
    "제목(한국어): {title_ko}\n"
    "제목(영어): {title_en}\n"
    "설명: {desc}\n\n"
    "== Amazon SP-API 구조화 데이터 ==\n"
    "{sp_facts}\n\n"
    "== 추출할 쿠팡 속성 스펙 (JSON) ==\n"
    "{specs}\n\n"
    "== 단위 변환표 (US → Metric) ==\n"
    "[무게]\n"
    "  1 oz (ounce) = 28.35 g  — 예) 2.1 oz → 60g, 16 oz → 454g\n"
    "  1 lb (pound) = 453.6 g  — 예) 2 lbs → 907g, 5 lbs → 2.27kg\n"
    "  1 grain = 64.8 mg       — 예) 325 gr → 21g (약품)\n"
    "[길이]\n"
    "  1 inch (in, \") = 2.54 cm  — 예) 10 inch → 25.4cm\n"
    "  1 foot (ft, ') = 30.48 cm — 예) 6 ft → 182.9cm (반올림 183cm)\n"
    "  1 yard = 91.44 cm\n"
    "[용량(부피)]\n"
    "  1 fl oz = 29.57 ml        — 예) 8 fl oz → 237ml, 100 fl oz → 2957ml\n"
    "  1 cup = 236.6 ml\n"
    "  1 pint (pt) = 473.2 ml\n"
    "  1 quart (qt) = 946.4 ml\n"
    "  1 gallon (gal) = 3785 ml (3.785L)\n"
    "  1 tablespoon (tbsp) = 14.79 ml\n"
    "  1 teaspoon (tsp) = 4.93 ml\n\n"
    "== 개수/패키지 추론 패턴 ==\n"
    "  A. 'Serving Size: 2 capsules, 60 Servings per Container' → 2×60 = 120캡슐\n"
    "  B. 'Count: 180' 또는 'Bottle of 60' → 180개 / 60정\n"
    "  C. 'Pack of 3' + 각 병 '60 caps' → 총 180캡슐 (또는 세트 묻지 않으면 60캡슐 × 3팩)\n"
    "  D. '30-Day Supply' + '1 per day' → 30개\n"
    "  E. 'Item Weight: 2.1 ounces (60 g)' → 60g (metric 이미 있으면 그대로 사용)\n"
    "  F. 'Net Weight 100ml / 3.4 fl oz' → 100ml 직접 사용\n"
    "  G. '1 lb pouch, 2 pack' + 속성이 '개당 중량' 이면 453g, '총 중량' 이면 907g\n"
    "  H. '30 gummies' / '60 soft gels' / '90 tablets' → 모두 정/캡슐 개수로 인식\n\n"
    "== 규칙 ==\n"
    "1. SELECT 타입: allowedValues 중 **가장 적합한 한 값**만 정확히 복사.\n"
    "2. NUMBER + INPUT 타입: '숫자+단위' 형식. 단위는 **반드시 usableUnits 내 값**.\n"
    "   변환 후 소수 반올림은 **1자리** 유지 (예: 907.2g → 907g, 29.57ml → 30ml).\n"
    "3. STRING + INPUT 타입: 30자 이내.\n"
    "4. 우선순위: SP-API 구조화 데이터 > 제목 > 설명. metric 값이 이미 있으면 변환보다 우선.\n"
    "5. 개당/총 구분: 속성명이 '개당 X' 이면 1개 분량, '총 X' 또는 '전체 X' 이면 팩/세트 합산.\n"
    "6. **추출 근거가 전혀 없을 때만 null**. 변환·계산으로 도출 가능하면 값 제공.\n"
    "7. 최종 값은 반드시 usableUnits 표기에 맞게 (예: 'g' vs 'kg' 중 usableUnits 에 있는 것).\n\n"
    "응답은 JSON만, 다른 텍스트 없이:\n"
    "{{\"속성명1\": \"값1\", \"속성명2\": null}}\n"
)


def _build_strict_prompt(product: dict, sp_facts: dict, specs: list[dict]) -> str:
    title_ko = (product.get("title_ko") or "")[:300]
    title_en = (product.get("title_en") or "")[:300]
    desc = (product.get("description_ko") or product.get("description_en") or "")[:1200]
    return _STRICT_PROMPT_TMPL.format(
        title_ko=title_ko,
        title_en=title_en,
        desc=desc,
        sp_facts=json.dumps(sp_facts, ensure_ascii=False, indent=2) if sp_facts else "(SP-API 데이터 없음)",
        specs=json.dumps(specs, ensure_ascii=False),
    )


def _attr_specs_from_meta(attrs_meta: list[dict]) -> list[dict]:
    """build_required_attributes 와 동일한 spec 포맷."""
    specs = []
    for a in attrs_meta:
        spec = {
            "name": (a.get("attributeTypeName") or "").strip(),
            "dataType": a.get("dataType") or "STRING",
            "inputType": a.get("inputType") or "INPUT",
        }
        if spec["inputType"] == "SELECT":
            vals = []
            for v in (a.get("inputValues") or []):
                if isinstance(v, dict):
                    vals.append(v.get("attributeValueName"))
                else:
                    vals.append(str(v))
            spec["allowedValues"] = [v for v in vals if v]
        else:
            spec["basicUnit"] = a.get("basicUnit") or ""
            spec["usableUnits"] = list(a.get("usableUnits") or [])
        specs.append(spec)
    return specs


def extract_mandatory_strict(product_id: int) -> dict:
    """SP-API + 강화 프롬프트로 누락 MANDATORY 속성 재추출해 저장.

    Returns: {"product_id", "attempted":[name], "extracted":{name:value},
              "failed":[name], "skipped_saved":[name]}
    값은 products.coupang_attributes_json (dict) 에 머지 저장 (덮어쓰지 않음:
    기존 저장값은 유지). 복구(pending 전환)는 호출자가 별도 결정.
    """
    from backend.purchase.database import get_db
    from backend.purchase.services.coupang_meta import get_category_meta

    with get_db() as conn:
        p_row = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        l_row = conn.execute(
            "SELECT coupang_category_code FROM listings_pa WHERE product_id=? AND channel='coupang'",
            (product_id,),
        ).fetchone()
    if not p_row or not l_row or not l_row["coupang_category_code"]:
        return {"product_id": product_id, "attempted": [], "extracted": {},
                "failed": [], "skipped_saved": [], "error": "상품/카테고리 없음"}

    product = dict(p_row)
    meta = get_category_meta(str(l_row["coupang_category_code"]))
    if not meta:
        return {"product_id": product_id, "attempted": [], "extracted": {},
                "failed": [], "skipped_saved": [], "error": "카테고리 메타 조회 실패"}

    mandatory = [a for a in (meta.get("attributes") or []) if a.get("required") == "MANDATORY"]
    if not mandatory:
        return {"product_id": product_id, "attempted": [], "extracted": {},
                "failed": [], "skipped_saved": []}

    already_saved = _load_saved_coupang_attrs(product)
    target = [a for a in mandatory
              if (a.get("attributeTypeName") or "").strip() not in already_saved]
    skipped = [
        (a.get("attributeTypeName") or "").strip()
        for a in mandatory
        if (a.get("attributeTypeName") or "").strip() in already_saved
    ]

    if not target:
        return {"product_id": product_id, "attempted": [], "extracted": {},
                "failed": [], "skipped_saved": skipped}

    sp_facts = _fetch_sp_api_facts(product.get("asin") or "")
    specs = _attr_specs_from_meta(target)
    prompt = _build_strict_prompt(product, sp_facts, specs)

    # SP-API 수집 결과 요약 로깅 — "AI 문제 vs 원천 데이터 부족" 구분용
    logger.info(
        f"[strict] pid={product_id} SP-API keys={list(sp_facts.keys())} "
        f"target={[s['name'] for s in specs]}"
    )

    try:
        from backend_shared.ai.service import _call_gemini
        raw = _call_gemini(prompt, max_tokens=1200)
    except Exception as e:
        logger.warning(f"[strict] pid={product_id} Gemini 예외: {e}")
        raw = None

    if raw:
        flat = raw.strip().replace("\n", " ").replace("\r", " ")[:500]
        logger.info(f"[strict] pid={product_id} Gemini raw: {flat}")
    else:
        logger.warning(f"[strict] pid={product_id} Gemini 응답 없음/빈값")

    parsed: dict = {}
    if raw:
        m = re.search(r'\{[\s\S]*\}', raw)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"[strict] pid={product_id} JSON 파싱 실패")
                parsed = {}
        else:
            logger.warning(f"[strict] pid={product_id} Gemini 응답에 JSON 블록 없음")

    extracted: dict = {}
    failed: list[str] = []
    for a in target:
        name = (a.get("attributeTypeName") or "").strip()
        val = parsed.get(name) if isinstance(parsed, dict) else None
        if not isinstance(val, str) or not val.strip():
            logger.info(f"[strict] pid={product_id} attr='{name}' reject: AI null/빈값 (raw={val!r})")
            failed.append(name)
            continue
        if not _validate_ai_value(a, val):
            # validation 실패 사유 상세
            reason = _describe_validation_failure(a, val)
            logger.info(f"[strict] pid={product_id} attr='{name}' reject: {reason} (AI값={val!r})")
            failed.append(name)
            continue
        extracted[name] = val.strip()

    # 저장 머지 — products.coupang_attributes_json (dict, 래핑 키 없음)
    if extracted:
        try:
            existing_raw = product.get("coupang_attributes_json")
            coupang_attrs = json.loads(existing_raw) if existing_raw else {}
            if not isinstance(coupang_attrs, dict):
                coupang_attrs = {}
        except (json.JSONDecodeError, TypeError):
            coupang_attrs = {}
        coupang_attrs.update(extracted)
        with get_db() as conn:
            conn.execute(
                "UPDATE products SET coupang_attributes_json=? WHERE id=?",
                (json.dumps(coupang_attrs, ensure_ascii=False), product_id),
            )

    logger.info(
        f"[strict] pid={product_id} 시도 {len(target)} / 추출 {len(extracted)} / "
        f"실패 {len(failed)} / 기존저장 {len(skipped)}"
    )
    return {
        "product_id": product_id,
        "attempted": [s["name"] for s in specs],
        "extracted": extracted,
        "failed": failed,
        "skipped_saved": skipped,
    }
