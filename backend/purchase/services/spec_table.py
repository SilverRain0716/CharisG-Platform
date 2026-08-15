"""상세페이지 '세부 사항' 스펙표 렌더러 (PIL).

SP-API facts + products 컬럼 → 한글 라벨 스펙표 이미지(JPG) 생성.
build_payload(coupang_lister)가 호출해 상세 contents 에 삽입.
- 빈 값 자동 생략, 2행 미만이면 None(표 생략).
- 폰트: backend/purchase/assets/NanumGothic.ttf (없으면 /home/ubuntu/fonts 폴백).
"""
import os
import re
import json
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

_ASSETS = Path(__file__).resolve().parent.parent / "assets"
_FONT_CANDIDATES = [
    str(_ASSETS / "NanumGothic.ttf"),
    "/home/ubuntu/fonts/NanumGothic.ttf",
]

# SP-API key → 한글 라벨 (순서 유지)
_LABELS = [
    ("brand", "브랜드"),
    ("manufacturer", "제조사"),
    ("model_number", "모델명"),
    ("part_number", "제조사 부품번호"),
    ("color", "색상"),
    ("style", "스타일"),
    ("material", "소재"),
    ("size_label", "사이즈"),
    ("number_of_items", "수량"),
    ("product_type", "분류"),
]

# ── 차단 목록 (2026-08-08) ─────────────────────────────
# 내부코드·규제플래그·판매채널 메타·중복·★민감정보. 고객에게 의미가 없거나 노출하면 안 되는 것.
_BLOCK = {
    # ★민감 — 아마존 정가(USD). 노출 시 원가 역산 가능. 표본 96% 에 존재.
    "list_price",
    # 내부 코드 / 규제 플래그
    "unspsc_code", "skip_offer", "package_level", "product_site_launch_date", "street_date",
    "supplier_declared_dg_hz_regulation", "supplier_declared_has_product_identifier_exemption",
    "externally_assigned_product_identifier", "fcc_radio_frequency_emission_compliance",
    "gpsr_safety_attestation", "pesticide_marking", "ships_globally", "merchant_suggested_asin",
    "batteries_required", "batteries_included", "contains_liquid_contents", "compliance_media",
    "customer_package_type", "title_differentiation", "variation_theme",
    # 다른 블록에서 쓰거나 별도 처리
    "item_name", "bullet_point", "product_description",
    "item_dimensions", "item_package_dimensions", "item_length_width",
    "item_length_width_thickness", "display", "item_weight", "item_package_weight",
    # ★sp_api_facts_json 의 내부 정규화 키 — 원본 attributes 가 아니라서 1차에서 놓쳤다.
    #   그대로 두면 asin·이미지 URL·수집시각·영문 원문이 고객 상세표에 실린다.
    "asin", "parent_asin", "marketplace", "fetched_at", "sales_rank",
    "title_en", "description_en", "bullet_points", "images", "identifiers",
    "package_dimensions", "package_weight_g", "item_weight_g", "package_quantity",
    "variation_dimensions", "website_display_group_name", "size_attr",
    "unit_count_unit", "product_type", "item_type_keyword",
}

# ── 한글 라벨 사전 ─────────────────────────────────────
_LABEL = {
    "brand": "브랜드", "manufacturer": "제조사",
    "model_number": "모델명", "model_name": "모델명",
    "part_number": "제조사 부품번호", "oem_equivalent_part_number": "OEM 부품번호",
    "color": "색상", "material": "소재", "size": "사이즈", "style": "스타일",
    "pattern": "패턴", "exterior_finish": "외장 마감", "finish_type": "마감",
    "item_hardness": "경도", "clarity": "투명도", "screen_surface_description": "표면 처리",
    "body_shape": "형태", "theme": "테마",
    "number_of_items": "수량", "number_of_pieces": "개수", "unit_count": "개수",
    "item_package_quantity": "포장 수량", "number_of_packs": "팩 수", "number_of_boxes": "박스 수",
    "included_components": "구성품", "set_name": "세트 구성", "package_size_name": "패키지 크기",
    "browse_classification": "분류", "specific_uses_for_product": "용도",
    "size_label": "사이즈", "unit_count_value": "개수",
    "compatible_devices": "호환 기기", "compatible_phone_models": "호환 기종",
    "compatible_with_vehicle_type": "호환 차종", "compatibility_options": "호환 정보",
    "fit_type": "장착 방식", "automotive_fit_type": "차량 적합성",
    "auto_part_position": "장착 위치", "mounting_type": "거치 방식",
    "special_feature": "주요 기능", "water_resistance_level": "방수 등급",
    "is_assembly_required": "조립", "is_waterproof": "방수",
    "warranty_description": "보증", "country_of_origin": "원산지",
    "release_date": "출시일", "manufacturer_part_number": "제조사 부품번호",
    "age_range_description": "사용 연령", "target_audience_keyword": "대상",
    "ink": "잉크", "point": "펜촉", "line_size": "선 굵기",
    "writing_instrument_form": "형태", "surface_recommendation": "사용 가능 표면",
}

# ── 값 사전 (자주 나오는 영문 값) ──────────────────────
_VALUE = {
    "vehicle_specific_fit": "차종 전용", "custom fit": "차종 전용",
    "vehicle specific fit": "차종 전용", "universal_fit": "범용", "universal fit": "범용",
    "water_resistant": "생활방수", "waterproof": "방수", "not_water_resistant": "방수 아님",
    "glossy": "유광", "matte": "무광", "painted": "도장", "smooth": "매끈",
    "car": "자동차", "automobiles": "자동차용", "standard packaging": "일반 포장",
    "adult": "성인", "unisex-adult": "성인 공용", "kid": "아동",
    "chisel": "치즐(사각촉)", "fine": "가는촉", "bullet": "둥근촉", "water": "수성",
    "jumbo": "대용량",
    # 소재 (표본 900건 기준 56종 — 사실상 전부 덮인다)
    "tempered glass": "강화유리", "polyester": "폴리에스터", "rubber": "고무",
    "thermoplastic elastomer (tpe)": "TPE(열가소성 엘라스토머)", "tpe": "TPE",
    "abs plastic": "ABS 플라스틱", "acrylonitrile butadiene styrene": "ABS 수지",
    "leather": "가죽", "faux leather": "인조가죽", "pu leather": "PU 가죽",
    "plastic": "플라스틱", "acrylic": "아크릴", "metal": "금속", "silicone": "실리콘",
    "flannel": "플란넬", "high-grade flannel": "고급 플란넬",
    "aluminum": "알루미늄", "alloy steel": "합금강", "stainless steel": "스테인리스",
    "steel": "강철", "iron": "철", "copper": "구리", "zinc alloy": "아연합금",
    "carbon fiber": "카본", "carbon fiber pattern": "카본 패턴",
    "microfiber": "극세사", "nylon": "나일론", "cotton": "면", "linen": "린넨",
    "wool": "울", "silk": "실크", "velvet": "벨벳", "suede": "스웨이드",
    "glass": "유리", "ceramic": "세라믹", "wood": "원목", "bamboo": "대나무",
    "foam": "폼", "mesh": "메쉬", "eva": "EVA", "pvc": "PVC", "tpu": "TPU",
    "thermoplastic polyurethane (tpu)": "TPU(열가소성 폴리우레탄)", "nano": "나노",
    # 색상 (상위 빈도 위주)
    "black": "블랙", "white": "화이트", "red": "레드", "blue": "블루", "green": "그린",
    "yellow": "옐로우", "light yellow": "라이트 옐로우", "beige": "베이지",
    "gray": "그레이", "grey": "그레이", "silver": "실버", "gold": "골드",
    "brown": "브라운", "pink": "핑크", "purple": "퍼플", "orange": "오렌지",
    "navy": "네이비", "ivory": "아이보리", "khaki": "카키", "wine": "와인",
    "mint": "민트", "sky blue": "스카이블루", "multicolor": "멀티컬러",
    "transparent": "투명", "clear": "투명", "as shown": "사진과 동일",
    # 분류 (browse_classification 상위)
    "screen protector foils": "액정보호필름", "center consoles": "센터콘솔",
    "dash covers": "대시보드 커버", "condensers": "콘덴서", "floor mats": "바닥매트",
    "scents": "방향제", "blades": "와이퍼 블레이드", "starters": "스타터",
    "ball joints": "볼조인트", "trays & bags": "트레이·파우치",
    "timing belt tensioners": "타이밍벨트 텐셔너", "essential oil sets": "에센셜오일 세트",
    "hub assemblies": "허브 어셈블리", "key shells": "키 케이스",
    "cup holders": "컵홀더", "fuel injection": "연료 분사",
    "seat covers": "시트커버", "pen refills": "펜 리필",
    "balance shaft belt tensioners": "밸런스샤프트 텐셔너",
    # 기타
    "modern": "모던", "medium nib": "중간 촉",
}

# 값이 이러면 행 자체를 버린다 (정보가 없는 것과 같음)
_JUNK_VALUES = {"unknown", "not_applicable", "none", "n/a", "na", "-", "기타", "other", "others",
                "body", "as per picture", "default", "standard"}

# style 에 OE 부품번호·순수숫자가 들어오는 사례가 많다(표본상 55종 중 다수). 정보가 아니다.
_JUNK_STYLE_RE = re.compile(r"^(oe[:\s-]|#)?\s*[A-Za-z]{0,3}[\d\-_.]{3,}$", re.I)
# color 에 차종/연식이 들어오는 오염("2026 Toyota RAV4", "19-25 Toyota RAV4").
_VEHICLE_IN_COLOR_RE = re.compile(r"(\b(19|20)\d{2}\b|^\d{2}\s*[-~]\s*\d{2}\b)")

# 표시 우선순위 — 앞쪽일수록 위에 온다. 목록에 없으면 뒤에 알파벳순.
_ORDER = [
    "brand", "manufacturer", "model_number", "model_name", "part_number",
    "oem_equivalent_part_number", "item_type_keyword", "specific_uses_for_product",
    "color", "material", "size", "style", "pattern", "exterior_finish", "finish_type",
    "screen_surface_description", "item_hardness", "clarity",
    "number_of_items", "number_of_pieces", "unit_count", "included_components", "set_name",
    "item_package_quantity", "number_of_packs", "number_of_boxes", "package_size_name",
    "fit_type", "automotive_fit_type", "auto_part_position", "mounting_type",
    "compatible_with_vehicle_type", "compatible_devices", "compatible_phone_models",
    "compatibility_options",
    "special_feature", "water_resistance_level", "is_waterproof", "is_assembly_required",
    "ink", "point", "line_size", "writing_instrument_form", "surface_recommendation",
    "age_range_description", "target_audience_keyword", "country_of_origin",
    "warranty_description",
]

_MAX_ROWS = 18   # 표가 상세를 잡아먹지 않도록 상한


def _pretty_key(k: str) -> str:
    """사전에 없는 키 → 읽을 만한 라벨. (snake_case → 띄어쓰기 + 첫 글자 대문자)"""
    return k.replace("_", " ").strip().title()


def _norm_value(key: str, val):
    """값 정규화 — 불리언/숫자/영문값 처리. 버릴 값이면 None."""
    if val is None:
        return None
    if isinstance(val, bool):
        if key == "is_assembly_required":
            return "필요" if val else "불필요"
        if key == "is_waterproof":
            return "방수" if val else "비방수"
        return "예" if val else None            # False 는 정보가 아니라 판단 — 행 생략
    s = str(val).strip()
    if not s or s.lower() in _JUNK_VALUES:
        return None
    if s.lower() in ("true", "false"):
        return _norm_value(key, s.lower() == "true")
    # 2.0 → 2
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    hit = _VALUE.get(s.lower())
    if hit:
        return hit
    return _apply_patterns(s)


# ── 패턴형 값 (숫자가 끼어 사전으로 못 덮는 형태) ──────
_PATTERNS = (
    (re.compile(r"^pack\s*of\s*(\d+)$", re.I),            lambda m: f"{m.group(1)}개입"),
    (re.compile(r"^(\d+)\s*(?:pcs?|pieces?|count|ea)$", re.I), lambda m: f"{m.group(1)}개"),
    (re.compile(r"^(\d+)\s*seats?$", re.I),                lambda m: f"{m.group(1)}인승"),
    (re.compile(r"^(\d+)\s*pack$", re.I),                  lambda m: f"{m.group(1)}팩"),
    (re.compile(r"^(\d+)\s*day\s*manufacturer$", re.I),    lambda m: f"제조사 보증 {m.group(1)}일"),
    (re.compile(r"^(\d+)\s*year\s*manufacturer$", re.I),   lambda m: f"제조사 보증 {m.group(1)}년"),
    (re.compile(r"^(\d+)\s*days?$", re.I),                 lambda m: f"{m.group(1)}일"),
    (re.compile(r"^(\d+)\s*years?$", re.I),                lambda m: f"{m.group(1)}년"),
    (re.compile(r"^(\d+)\s*months?$", re.I),               lambda m: f"{m.group(1)}개월"),
    (re.compile(r"^lifetime$", re.I),                      lambda m: "평생 보증"),
)


def _apply_patterns(s: str) -> str:
    for rx, fn in _PATTERNS:
        m = rx.match(s.strip())
        if m:
            return fn(m)
    return s



def _font(size):
    for p in _FONT_CANDIDATES:
        if os.path.isfile(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _facts_for(product_id):
    """products 행 + sp_api_facts_json. 비어있으면 SP-API 재조회+캐시."""
    from backend.purchase.database import get_db
    with get_db() as c:
        r = c.execute(
            "SELECT asin, brand, amazon_manufacturer, identifiers_json, "
            "dimensions_json, weight_g, sp_api_facts_json, sp_raw_json FROM products WHERE id=?",
            (product_id,)).fetchone()
    if not r:
        return None, None
    facts = {}
    if r["sp_api_facts_json"]:
        try:
            facts = json.loads(r["sp_api_facts_json"]) or {}
        except Exception:
            facts = {}
    # 풍부한 속성 없으면 SP-API 재조회 + 캐시 (리스팅당 1회)
    if not (facts.get("model_number") or facts.get("material") or facts.get("style")):
        try:
            from backend.purchase.services.sp_api_facts import fetch_full_catalog_facts
            f2 = fetch_full_catalog_facts(r["asin"])
            if f2:
                facts = {**f2, **facts} if facts else f2
                with get_db() as c:
                    c.execute("UPDATE products SET sp_api_facts_json=? WHERE id=?",
                              (json.dumps(facts, ensure_ascii=False), product_id))
                    c.commit()
        except Exception as e:
            logger.warning(f"[spec_table] {product_id} facts 재조회 실패: {e}")
    return facts, r


# ---- 데이터 위생 게이트 (허위치수/오염값 유출 방지) ----
_BAD_COLOR_TOKENS = ("pcs", "pack", "count", "개입", "set", "piece", "묶음", "팩", " ea", "ea ")


def _clean_color(val):
    """색상값이 수량/팩/변형명/치수로 오염됐으면 None."""
    if not isinstance(val, str):
        return None
    s = val.strip()
    if not s or s.isdigit():
        return None
    if _VEHICLE_IN_COLOR_RE.search(s):       # 색상 칸에 차종/연식이 들어온 오염
        return None
    low = s.lower()
    if any(t in low for t in _BAD_COLOR_TOKENS):
        return None
    if any(ch.isdigit() for ch in s) and ('"' in s or "cm" in low or "mm" in low or "inch" in low):
        return None
    return s


def _is_dim_string(s):
    """'25" x 17.8" x 3"' 같은 치수 문자열인가 (영문 인치/중복 치수 제거용). 'Large','XL' 등 단어 사이즈는 False."""
    if not isinstance(s, str):
        return False
    if '"' in s or "''" in s:
        return True
    return bool(re.search(r"\d+(?:\.\d+)?\s*[x×*]\s*\d", s))


def _parse_dim_cm(d):
    if not isinstance(d, dict):
        return None
    try:
        l = float(d.get("length_cm") or 0)
        w = float(d.get("width_cm") or 0)
        h = float(d.get("height_cm") or 0)
    except Exception:
        return None
    return (l, w, h) if (l > 0 and w > 0 and h > 0) else None


def _is_round_inch(t):
    """치수가 전부 정확한 0.5인치 격자 = 측정값 아닌 제조스펙/포장표기 의심."""
    for cm in t:
        inch = cm / 2.54
        if abs(inch - round(inch * 2) / 2) > 0.03:
            return False
    return True


def _fits_in(it, pk):
    """item이 package 안에 실제로 들어가는가(방향 무관)."""
    a, b = sorted(it, reverse=True), sorted(pk, reverse=True)
    return all(a[i] <= b[i] * 1.02 for i in range(3))


def _safe_item_dims(facts):
    """'제품 실측'으로 신뢰 가능한 치수만 (l,w,h) 반환, 아니면 None.
    멀티팩/포장치수/스펙표기 의심은 전부 제외 — 쿠팡 허위·과장(계정 리스크) 차단."""
    it = _parse_dim_cm(facts.get("item_dimensions"))
    if not it:
        return None
    ni = facts.get("number_of_items")
    if ni is None:
        ni = facts.get("unit_count_value")
    try:
        if ni is not None and float(ni) > 1:   # 멀티팩 → item_dimensions = 포장/카드 치수
            return None
    except Exception:
        pass
    if max(it) > 300:                           # 비현실적 크기
        return None
    pk = _parse_dim_cm(facts.get("package_dimensions"))
    if not pk:                                  # 대조군 없으면 신뢰 불가
        return None
    if not _fits_in(it, pk):                    # 박스에 안 들어감 = 데이터 오류
        return None
    if it[0] * it[1] * it[2] > pk[0] * pk[1] * pk[2] * 0.85:  # 여유부피 없음 = item이 곧 포장
        return None
    if _is_round_inch(it):                       # 정확한 인치격자 = 표기값
        return None
    return it


def build_spec_rows(product_id):
    """(label, value) 리스트 — 빈 값 생략, 라벨 중복 제거."""
    facts, r = _facts_for(product_id)
    if facts is None:
        return []
    facts = facts or {}
    rows = []
    _brand_low = ((facts.get("brand") or (r["brand"] if r else "") or "")).strip().lower()
    _model_val = None

    # ★2026-08-08: 화이트리스트 10개 → 블랙리스트 통과 방식. 카테고리별 속성이 그대로 살아난다.
    #   출처는 facts(정규화본) + sp_raw_json.attributes(원본) 합집합.
    pool = dict(facts or {})
    try:
        _raw_all = json.loads((r["sp_raw_json"] if r and "sp_raw_json" in r.keys() else None) or "{}")
        for _k, _v in (_raw_all.get("attributes") or {}).items():
            if _k in pool:
                continue
            if isinstance(_v, list) and _v and isinstance(_v[0], dict):
                _v = _v[0].get("value")
            if isinstance(_v, (str, int, float, bool)):
                pool[_k] = _v
    except Exception:
        pass

    def _rank(k):
        return _ORDER.index(k) if k in _ORDER else len(_ORDER) + 1

    seen_vals = set()
    for k in sorted(pool, key=lambda x: (_rank(x), x)):
        if k in _BLOCK or k.startswith(("sp_", "_")):
            continue
        v = pool.get(k)
        if k == "color":
            v = _clean_color(v)
        elif k == "style" and isinstance(v, str) and _JUNK_STYLE_RE.match(v.strip()):
            v = None                                  # OE 부품번호/숫자만 → 정보 아님
        elif k in ("size", "size_label") and _is_dim_string(v):
            v = None                                  # 영문 인치 치수 → 아래 '상품 크기'(cm)가 대신한다
        elif k in ("model_number", "model_name", "part_number", "manufacturer"):
            if isinstance(v, str) and _brand_low and v.strip().lower() == _brand_low:
                v = None                              # 브랜드명 중복 → 무의미한 행
        if k in ("part_number", "model_name") and isinstance(v, str) and _model_val \
                and v.strip() == _model_val:
            v = None                                  # 모델명과 동일값 → 중복 행 생략
        sval = _norm_value(k, v)
        if not sval:
            continue
        sval = sval[:60]
        if sval.lower() in seen_vals:                 # 라벨만 다르고 값이 같은 행 제거
            continue
        seen_vals.add(sval.lower())
        rows.append((_LABEL.get(k) or _pretty_key(k), sval))
        if k in ("model_number", "model_name"):
            _model_val = sval
    # ★구성품(included_components) — sp_raw_json 원본 attributes 에서 추출 (set_name 폴백)
    try:
        _raw = json.loads((r["sp_raw_json"] if r and "sp_raw_json" in r.keys() else None) or "{}")
        _attrs = _raw.get("attributes") or {}
        def _a0(_k):
            _v = _attrs.get(_k)
            return _v[0].get("value") if isinstance(_v, list) and _v and isinstance(_v[0], dict) else None
        _comp = _a0("included_components") or _a0("set_name")
        if _comp and str(_comp).strip():
            rows.append(("구성품", str(_comp).strip()[:70]))
    except Exception:
        pass
    # 치수 — 허위 방지 게이트 통과시에만(멀티팩/포장치수/스펙표기 전부 제외), length_cm 버그 수정
    safe = _safe_item_dims(facts)
    if safe:
        def _fd(x):  # 1cm 미만은 0 대신 소수 1자리
            return str(int(round(x))) if round(x) >= 1 else f"{x:.1f}"
        rows.append(("상품 크기", f"{_fd(safe[0])}×{_fd(safe[1])}×{_fd(safe[2])} cm"))
    # 무게
    w = facts.get("item_weight_g") or (r["weight_g"] if r else None)
    if w:
        try:
            w = float(w)
            rows.append(("무게", f"{round(w)}g" if w < 1000 else f"{w/1000:.1f}kg"))
        except Exception:
            pass
    # 바코드(UPC/EAN)
    try:
        ids = json.loads((r["identifiers_json"] if r else None) or "[]")
        upc = next((x.get("value") for x in (ids if isinstance(ids, list) else [])
                    if str(x.get("type", "")).upper() in ("UPC", "EAN", "GTIN")), None)
        if upc:
            rows.append(("바코드", str(upc)))
    except Exception:
        pass
    # 라벨 중복 제거
    seen, out = set(), []
    for lab, v in rows:
        if lab not in seen:
            seen.add(lab)
            out.append((lab, v))
    return out[:_MAX_ROWS]


def render_spec_table(product_id):
    """스펙표 JPG 렌더 → public_url 반환. 2행 미만이면 None."""
    rows = build_spec_rows(product_id)
    if len(rows) < 2:
        return None

    W = 860
    title_f = _font(30)
    head_f = _font(22)
    cell_f = _font(21)
    pad, row_h, label_w = 28, 52, 230
    H = 36 + 46 + 16 + 46 + len(rows) * row_h + 36
    img = Image.new("RGB", (W, H), "#ffffff")
    d = ImageDraw.Draw(img)

    def btext(xy, txt, font, fill):  # faux-bold
        d.text(xy, txt, font=font, fill=fill)
        d.text((xy[0] + 1, xy[1]), txt, font=font, fill=fill)

    y = 36
    btext((pad, y), "세부 사항", title_f, "#1B3A5C"); y += 46
    d.line((pad, y, W - pad, y), fill="#E8845A", width=3); y += 16
    d.rectangle((pad, y, W - pad, y + 46), fill="#1B3A5C")
    btext((pad + 16, y + 11), "항목", head_f, "#ffffff")
    btext((pad + label_w + 16, y + 11), "설명", head_f, "#ffffff"); y += 46
    for i, (lab, val) in enumerate(rows):
        bg = "#F7F5F0" if i % 2 else "#ffffff"
        d.rectangle((pad, y, W - pad, y + row_h), fill=bg, outline="#E5E0D8")
        d.line((pad + label_w, y, pad + label_w, y + row_h), fill="#E5E0D8")
        btext((pad + 16, y + 15), lab, cell_f, "#1B3A5C")
        # ★값이 길면 표 밖으로 넘치던 문제(2026-08-08) — 실제 렌더 폭 기준 말줄임.
        _avail = (W - pad) - (pad + label_w + 16) - 12
        _v = val
        while _v and d.textlength(_v, font=cell_f) > _avail:
            _v = _v[:-1]
        if _v != val:
            _v = _v[:-1] + "…"
        d.text((pad + label_w + 16, y + 15), _v, font=cell_f, fill="#444444")
        y += row_h

    out_dir = Path(__file__).resolve().parent.parent / "media" / "products" / str(product_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    img.save(str(out_dir / "spec.jpg"), "JPEG", quality=90, optimize=True)
    return f"/api/pa/images/products/{product_id}/spec.jpg"
