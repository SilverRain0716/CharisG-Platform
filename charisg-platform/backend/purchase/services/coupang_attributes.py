"""
coupang_attributes.py — 쿠팡 카테고리별 MANDATORY 속성 자동 주입 (v2).

**정책 (2026-04-20 재설계)**:
  - NUMBER 타입 (용량/중량/길이/개당용량/개당중량/가로길이/신발사이즈)
    → 주문 정확도 직결. 제목 추출 실패 시 skip (excluded).
  - 색상: soft fallback "혼합색상" (주문 영향 적음)
  - 수량: soft fallback "1개" (대부분 단일 판매)
  - 사이즈:
     * **프리사이즈 허용 카테고리** (가변/조절형) — 스포츠/레져, 반려, 패션, 뷰티,
       주방(소형), 완구, 욕실잡화, 식품 → 추출 실패 시 '프리사이즈' fallback
     * **치수 strict 카테고리** — 가구/홈데코, 가전, 출산/유아동(안전),
       도서/문구/오피스 → 추출 실패 시 skip
  - oz/온스 → ml 변환 (쿠팡은 usableUnits 외 단위 거부). 20oz → 591ml.
  - 제목은 손대지 않음 — 중복 단어 경고는 수용 (데이터 정확도 우선).

**반환**:
  build_required_attributes(meta, product) -> tuple[list[dict], str]
      (attributes, skip_reason)
      skip_reason이 있으면 상품 전체를 skip 처리.
"""
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
# 우선순위: 개 > 팩 > 세트 > 켤레 > pack/pk/pcs/set
_QTY_UNITS_KO = ("개입", "개", "팩", "세트", "켤레", "짝")


def _extract_quantity(title: str, basic_unit: str) -> Optional[str]:
    if not title:
        return None
    # 한글 단위
    for unit in _QTY_UNITS_KO:
        m = re.search(rf"(\d+)\s*{unit}(?![\w가-힣])", title)
        if m:
            return f"{m.group(1)}{unit}"
    # 영문 단위 (basic_unit로 통일)
    m = re.search(r"(\d+)\s*(?:pack|pk|pcs|set|packs)(?![\w가-힣])", title, flags=re.IGNORECASE)
    if m:
        return f"{m.group(1)}{basic_unit or '개'}"
    return None


# ── 용량 추출 (oz→ml 변환) ──────────────────
# 1 oz = 29.5735 ml
def _extract_volume(title: str, usable_units: tuple) -> Optional[str]:
    if not title:
        return None
    # oz / 온스 → ml (또는 L) 변환
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:oz|온스)", title, flags=re.IGNORECASE)
    if m:
        oz = float(m.group(1))
        ml = round(oz * 29.5735)
        if "ml" in usable_units:
            return f"{ml}ml"
        if "L" in usable_units:
            L = ml / 1000
            return f"{L:g}L"
        return f"{ml}ml"  # 기본 ml
    # ml 직접
    m = re.search(r"(\d+(?:\.\d+)?)\s*ml", title, flags=re.IGNORECASE)
    if m:
        val = float(m.group(1))
        if "ml" in usable_units:
            return f"{int(val) if val == int(val) else val}ml"
        if "L" in usable_units:
            L = val / 1000
            return f"{L:g}L"
    # L 직접
    m = re.search(r"(\d+(?:\.\d+)?)\s*L(?![\w가-힣])", title)
    if m:
        L = float(m.group(1))
        if "L" in usable_units:
            return f"{L:g}L"
        if "ml" in usable_units:
            return f"{int(L * 1000)}ml"
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


# ── 핵심 빌더 ──────────────────────────────
def build_required_attributes(
    meta: Optional[dict], product: Optional[dict] = None, cat_path: str = "",
) -> tuple[list[dict], str]:
    """MANDATORY 속성 리스트 + skip 사유 반환.

    Args:
        meta: 쿠팡 카테고리 메타 응답.
        product: products 행 dict (title_ko, title_en 사용).
        cat_path: 쿠팡 카테고리 path (예: '스포츠/레져 > 헬스/요가 > ...').
                  사이즈 soft/strict 분기에 사용.

    skip 사유가 비어있지 않으면 이 상품은 등록하지 말고 excluded 처리해야 함.
    """
    if not meta:
        return [], ""

    attrs_meta = meta.get("attributes") or []
    title = ""
    if product:
        title = f"{product.get('title_ko') or ''} {product.get('title_en') or ''}".strip()

    soft_size = _is_soft_size_path(cat_path)

    result = []
    for a in attrs_meta:
        if a.get("required") != "MANDATORY":
            continue
        name = (a.get("attributeTypeName") or "").strip()
        if not name:
            continue
        data_type = a.get("dataType") or "STRING"
        input_type = a.get("inputType") or "INPUT"
        input_values = a.get("inputValues") or []
        basic_unit = a.get("basicUnit") or ""
        usable_units = tuple(a.get("usableUnits") or [])

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
        if value is None:
            return [], f"MANDATORY '{name}' 정보 부족 (제목에서 추출 실패)"

        result.append({
            "attributeTypeName": name,
            "attributeValueName": value,
            "exposed": a.get("exposed", "EXPOSED"),
            "editable": True,
        })
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
    # 2) STRING — 종류별 추출/기본값
    if data_type == "STRING":
        # 색상: 추출 실패 시 "혼합색상" soft fallback
        if "색상" in name or "컬러" in name or "칼라" in name:
            return _extract_color(title) or "혼합색상"
        # 패션 사이즈: 이미 path가 "패션의류잡화" prefix인 경우 soft_size=True → 프리사이즈 OK
        if "패션" in name and "사이즈" in name:
            return _extract_size(title) or "프리사이즈"
        # 신발 사이즈: 추출 필수 (숫자) — skip이 안전
        if "신발" in name and "사이즈" in name:
            return _extract_size(title)
        # 일반 사이즈: path 기반 soft/strict 분기
        if "사이즈" in name or "치수" in name:
            extracted = _extract_size(title)
            if extracted:
                return extracted
            # 프리사이즈 허용 카테고리만 soft fallback
            return "프리사이즈" if soft_size else None
        # 구성품: 모호하므로 기본값 OK
        if "구성품" in name:
            return "기본 구성"
        # 모델명/품번: 추출 로직 없음 → skip
        if "모델명" in name or "품번" in name:
            return None
        # 도서(저자/출판사/ISBN): 구매대행 대상 아님, skip
        if name in ("저자", "출판사", "ISBN"):
            return None
        # 기타 STRING: skip
        return None

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
        # 용량: strict — 추출 실패 시 skip
        if "용량" in name or "부피" in name or "개당 용량" in name:
            return _extract_volume(title, usable_units)  # None이면 skip
        # 중량: strict
        if "중량" in name or "무게" in name or "개당 중량" in name:
            return _extract_weight(title, usable_units)
        # 길이류 (가로길이/세로길이/높이 등): strict
        if "길이" in name or "높이" in name or "폭" in name or "너비" in name:
            return _extract_length(title, basic_unit)
        # 기타 NUMBER: skip
        return None

    return None
