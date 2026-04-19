"""
coupang_attributes.py — 쿠팡 카테고리별 MANDATORY 속성 자동 주입.

쿠팡은 카테고리마다 1~5개의 필수 속성(색상/사이즈/수량 등)을 요구하며,
미제출 시 승인 반려됨. 메타 API에서 스키마를 받아 기본값을 채운다.

값 결정 순서:
  1. SELECT(ENUM) 타입인 경우: inputValues 중 첫 번째 사용 (쿠팡이 허용하는 값 확정)
  2. INPUT 타입인 경우: 속성 이름별 휴리스틱 (색상→"혼합색상", 수량→"1개" 등)
  3. 그 외 NUMBER/STRING: 안전 기본값 "기타" 또는 "1"

향후 개선 (Phase 2):
  - products.title 에서 정규식으로 색상/사이즈 추출
  - AI 배치 추론 (같은 카테고리끼리 묶어 1회 호출)
  - products.inferred_attributes_json (네이버 추론) → 쿠팡 name 매칭
"""
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


# ── 속성 이름 → STRING 타입 기본값 휴리스틱 ──────────────
# NUMBER 타입은 별도 처리 (basicUnit 사용).
_STRING_DEFAULTS_BY_NAME = (
    (("색상", "컬러", "칼라"), "혼합색상"),
    (("사이즈", "치수"), "프리사이즈"),
    (("재질", "소재"), "기타소재"),
    (("형태", "모양", "타입"), "기타"),
    (("성별",), "공용"),
    (("연령", "대상"), "성인용"),
    (("원산지",), "미국"),
)


def _default_for_name(name: str, data_type: str, basic_unit: str) -> str:
    """속성 이름·타입에 맞는 기본값 반환.

    NUMBER: '1' + basicUnit (예: '1ml', '1개', '1kg') — 쿠팡은 usableUnits에서만 단위 허용.
    STRING: 이름 기반 휴리스틱, 매칭 실패 시 '기타'.
    """
    if not name:
        return ""
    if data_type == "NUMBER":
        # basicUnit이 '없음'/빈값이면 단위 없는 숫자만
        if basic_unit and basic_unit != "없음":
            return f"1{basic_unit}"
        return "1"
    # STRING
    for keywords, default in _STRING_DEFAULTS_BY_NAME:
        if any(kw in name for kw in keywords):
            return default
    return "기타"


# ── 상품명에서 색상/사이즈 추출 (간단 버전) ──────────────
# 실패 시 None 반환하여 휴리스틱 기본값으로 폴백.
_COLOR_WORDS_KO = (
    "블랙", "화이트", "레드", "블루", "그린", "옐로우", "핑크", "퍼플", "브라운",
    "그레이", "실버", "골드", "베이지", "네이비", "민트", "카키", "아이보리",
    "검정", "흰색", "빨강", "파랑", "초록", "노랑", "회색", "남색",
)


def _extract_color(title: str) -> Optional[str]:
    if not title:
        return None
    # 한글 색상 단어
    for kw in _COLOR_WORDS_KO:
        if kw in title:
            return kw
    # 영문 색상 단어 (Black, White 등)
    m = re.search(r"\b(black|white|red|blue|green|yellow|pink|purple|brown|gray|grey|silver|gold|beige|navy)\b",
                  title, flags=re.IGNORECASE)
    if m:
        return m.group(1).capitalize()
    return None


def _extract_size(title: str) -> Optional[str]:
    if not title:
        return None
    # 인치 (예: 16온스, 3.1인치x2.4인치)
    m = re.search(r"(\d+(?:\.\d+)?\s*(?:인치|cm|mm|m|온스|oz|L|리터))", title, flags=re.IGNORECASE)
    if m:
        return m.group(1).replace(" ", "")
    # XL / L / M / S / XS
    m = re.search(r"\b(XXL|XL|XS|S|M|L)\b", title)
    if m:
        return m.group(1).upper()
    return None


# ── 핵심: 속성 페이로드 빌드 ──────────────────────
def build_required_attributes(meta: Optional[dict], product: Optional[dict] = None) -> list[dict]:
    """카테고리 메타에서 MANDATORY 속성만 추려 payload 엔트리 리스트로 반환.

    엔트리 형태: {attributeTypeName, attributeValueName, exposed, editable}
    """
    if not meta:
        return []
    attrs_meta = meta.get("attributes") or []
    title = ""
    if product:
        title = f"{product.get('title_ko') or ''} {product.get('title_en') or ''}".strip()

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

        value = ""
        # 1) SELECT(ENUM): 첫 번째 허용값 사용
        if input_type == "SELECT" and input_values:
            v0 = input_values[0]
            value = v0.get("attributeValueName") if isinstance(v0, dict) else str(v0)
        # 2) 휴리스틱 기본값만 사용.
        #    제목 기반 추출은 제거 — 쿠팡 validator가 "상품명과 옵션 중복 단어" 경고를 띄워
        #    승인요청이 차단되는 문제 발생. 캡처 페이로드도 '단일색상'/'프리사이즈' 같은
        #    일반값을 사용.
        if not value:
            value = _default_for_name(name, data_type, basic_unit)
        if not value:
            continue

        result.append({
            "attributeTypeName": name,
            "attributeValueName": value,
            "exposed": a.get("exposed", "EXPOSED"),
            "editable": True,
        })
    return result
