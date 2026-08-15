"""
sp_api_facts.py — Amazon SP-API CatalogItems 단일 호출 + 정규화 + DB 캐시.

배경 (v17 마이그레이션 참조):
  소싱 / 이미지 / 사후 strict 보정 등 여러 곳에서 CatalogItems 를 부분적으로 호출하던
  것을 단일 모듈로 통합. 한 번 호출에 summaries / attributes / images / dimensions /
  productTypes / identifiers / relationships / salesRanks 모두 받아 정규화 후
  products.sp_api_facts_json 에 캐시. 호출처는 캐시를 우선 조회하므로 같은 ASIN 에
  대해 7일 내 중복 호출 0.

정규화 dict 구조: 함수 docstring 참조.

호환성:
  - coupang_attributes._fetch_sp_api_facts 가 사용하던 free-form dict 형식이 아닌
    정규화 표준 dict 를 반환. 호출처(특히 Gemini 프롬프트 dump)는 키 이름이 더
    명시적이라 그대로 해석 가능.
  - DS asin_matching_service 는 검색 endpoint 라 별개 — 건드리지 않음.
"""
from __future__ import annotations

import re

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# CatalogItems 호출 간격 (TPS 2 한도 보수적 — 실제 0.5초/req = 2 RPS)
_SP_API_INTERVAL_SEC = 0.5

# 캐시 TTL — Amazon 카탈로그는 변동 적어 7일이면 충분
_CACHE_TTL_DAYS = 7


# ── 단위 변환 헬퍼 ─────────────────────────────────────────
_LB_TO_G = 453.592
_OZ_TO_G = 28.3495
_KG_TO_G = 1000.0
_INCH_TO_CM = 2.54
_FT_TO_CM = 30.48
_MM_TO_CM = 0.1
_FL_OZ_TO_ML = 29.5735


def _to_grams(value, unit: str) -> Optional[float]:
    """무게 단위를 g로 변환. 변환 불가 시 None."""
    if value is None or unit is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    u = (unit or "").lower().strip()
    if u in ("g", "gram", "grams"):
        return round(v, 1)
    if u in ("kg", "kilogram", "kilograms"):
        return round(v * _KG_TO_G, 1)
    if u in ("lb", "lbs", "pound", "pounds"):
        return round(v * _LB_TO_G, 1)
    if u in ("oz", "ounce", "ounces"):
        return round(v * _OZ_TO_G, 1)
    return None


def _to_cm(value, unit: str) -> Optional[float]:
    """길이 단위를 cm로 변환. 변환 불가 시 None."""
    if value is None or unit is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    u = (unit or "").lower().strip()
    if u in ("cm", "centimeter", "centimeters"):
        return round(v, 2)
    if u in ("mm", "millimeter", "millimeters"):
        return round(v * _MM_TO_CM, 2)
    if u in ("inch", "inches", "in"):
        return round(v * _INCH_TO_CM, 2)
    if u in ("ft", "foot", "feet"):
        return round(v * _FT_TO_CM, 2)
    if u in ("m", "meter", "meters"):
        return round(v * 100, 2)
    return None


def _to_ml(value, unit: str) -> Optional[float]:
    """부피 단위를 ml로 변환. kg/g 단위가 들어오면 None (Amazon이 net_content_volume에
    무게 단위를 넣는 경우 있음 — 호출자가 별도 처리)."""
    if value is None or unit is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    u = (unit or "").lower().strip()
    if u in ("ml", "milliliter", "milliliters"):
        return round(v, 1)
    if u in ("l", "liter", "liters"):
        return round(v * 1000, 1)
    if u in ("fl_oz", "fl oz", "fluid_ounce", "fluid_ounces", "fluid ounce", "fluid ounces"):
        return round(v * _FL_OZ_TO_ML, 1)
    return None


def _first_attr_value(attrs: dict, key: str):
    """attributes[key] 가 [{"value": v, "unit": u, ...}] 또는 [{"value": v, ...}] 형식.
    첫 번째 항목 반환 (없으면 None)."""
    val = attrs.get(key)
    if isinstance(val, list) and val:
        return val[0]
    if isinstance(val, dict):
        return val
    return None


# ── 정규화 추출 ─────────────────────────────────────────────
def _extract_from_summaries(summaries: list) -> dict:
    if not summaries:
        return {}
    s = summaries[0] if isinstance(summaries, list) else summaries
    if not isinstance(s, dict):
        return {}
    out = {}
    for k_src, k_dst in [
        ("itemName", "title_en"),
        ("brand", "brand"),
        ("manufacturer", "manufacturer"),
        ("modelNumber", "model_number"),
        ("partNumber", "part_number"),
        ("color", "color"),
        ("size", "size_label"),
        ("style", "style"),
        ("releaseDate", "release_date"),
        ("websiteDisplayGroupName", "website_display_group_name"),
    ]:
        v = s.get(k_src)
        if v:
            out[k_dst] = v
    pq = s.get("packageQuantity")
    if isinstance(pq, (int, float)):
        out["package_quantity"] = int(pq)
    bc = s.get("browseClassification")
    if isinstance(bc, dict) and bc.get("displayName"):
        out["browse_classification"] = bc["displayName"]
    return out


def _extract_dimensions(dimensions: list) -> dict:
    """SP-API dimensions: [{"marketplaceId": ..., "item": {...}, "package": {...}}]
    각 scope (item/package) 의 length/width/height/weight 를 cm/g 로 정규화.
    """
    if not dimensions:
        return {}
    out = {}
    for d in dimensions:
        if not isinstance(d, dict):
            continue
        for scope in ("item", "package"):
            sdata = d.get(scope)
            if not isinstance(sdata, dict):
                continue
            block = {}
            for axis in ("length", "width", "height"):
                v = sdata.get(axis)
                if isinstance(v, dict):
                    cm = _to_cm(v.get("value"), v.get("unit"))
                    if cm is not None:
                        block[f"{axis}_cm"] = cm
            wv = sdata.get("weight")
            if isinstance(wv, dict):
                g = _to_grams(wv.get("value"), wv.get("unit"))
                if g is not None:
                    block["weight_g"] = g
            if block:
                out[f"{scope}_dimensions"] = block
        break  # 첫 marketplace 한 행이면 충분
    return out


# ── 도서 전용 필드 (2026-08-01) ─────────────────────────────
# SP-API attributes 에 author/binding/edition/publication_date 가 오는데
# 기존 추출이 이를 버렸다. 도서 상품명 조립(원제+저자+제본+판차)에 필요.
_BOOK_ATTR_MAP = (
    ("author", "book_authors"),                 # [{"value":"Phillips, Mark"}, ...]
    ("binding", "book_binding"),                # paperback / hardcover
    ("edition", "book_edition"),
    ("publication_date", "book_publication_date"),
    ("publisher", "book_publisher"),
    ("number_of_pages", "book_pages"),
    ("languages", "book_languages"),
)


def _norm_author(v: str) -> str:
    """'Phillips, Mark' → 'Mark Phillips'. 콤마 없으면 원문 유지."""
    v = (v or "").strip()
    if "," in v:
        parts = [p.strip() for p in v.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            return f"{parts[1]} {parts[0]}"
    return v


# ── 연령·어린이제품 안전 속성 (2026-08-06 신설) ──────────────
# KC 어린이제품(만13세=156개월) 판정에 쓴다. 아마존이 완구 카테고리에 요구하는
# 속성이라 완구·게임·유아 유형에 주로 들어온다. 비완구 유형은 비어 있는 게 정상이다.
_AGE_WORD = {
    "baby": 0, "infant": 0, "newborn": 0, "toddler": 12, "preschool": 36,
    "kid": 36, "kids": 36, "child": 36, "children": 36, "youth": 96,
    "teen": 156, "adult": 216,
}


def _parse_age_desc(s: str):
    """'15+' → 180, '8-12' → 96, '8 years and up' → 96, 'Adult' → 216 (개월)."""
    if not s:
        return None
    t = str(s).strip().lower()
    m = re.search(r"(\d{1,3})\s*(?:\+|-|\s*years?|\s*yrs?|$)", t)
    if m:
        try:
            y = int(m.group(1))
            if 0 < y <= 21:
                return y * 12
            if 21 < y <= 260:      # 이미 개월 단위로 온 경우
                return y
        except ValueError:
            pass
    for w, mo in _AGE_WORD.items():
        if re.search(r"\b" + w + r"\b", t):
            return mo
    return None


def _extract_age_fields(attrs: dict) -> dict:
    """연령 7종 추출 + 낮은 연령 우선으로 kc_min_age_months 도출."""
    out = {}
    if not isinstance(attrs, dict):
        return out

    def _vals(key):
        raw = attrs.get(key)
        if raw is None:
            return []
        if isinstance(raw, list):
            return [x.get("value") if isinstance(x, dict) else x for x in raw]
        if isinstance(raw, dict):
            return [raw.get("value")]
        return [raw]

    nums = []
    v = _vals("manufacturer_minimum_age")
    v = [x for x in v if isinstance(x, (int, float))]
    if v:
        out["age_min_months"] = float(min(v))
        nums.append(float(min(v)))
    v = _vals("manufacturer_maximum_age")
    v = [x for x in v if isinstance(x, (int, float))]
    if v:
        out["age_max_months"] = float(max(v))

    descs = [str(x) for x in _vals("age_range_description") if x not in (None, "")]
    if descs:
        out["age_range_desc"] = " | ".join(descs)[:120]
        for d in descs:
            p = _parse_age_desc(d)
            if p is not None:
                nums.append(float(p))

    aud = [str(x) for x in _vals("target_audience_keyword") if x not in (None, "")]
    if aud:
        out["target_audience"] = " | ".join(aud)[:160]
    cps = [str(x) for x in _vals("cpsia_cautionary_statement") if x not in (None, "")]
    if cps:
        out["cpsia_warning"] = " | ".join(sorted(set(cps)))[:160]
    sw = [str(x) for x in _vals("safety_warning") if x not in (None, "")]
    if sw:
        out["safety_warning_text"] = sw[0][:240]

    # ★낮은 연령 우선. 숫자가 없을 때만 audience 로 판단한다.
    if nums:
        out["kc_min_age_months"] = min(nums)
    elif aud:
        low = " ".join(aud).lower()
        if re.search(r"\b(child|children|kid|kids|boy|boys|girl|girls|baby|infant|toddler)\b", low):
            out["kc_min_age_months"] = 36.0
        elif "adult" in low:
            out["kc_min_age_months"] = 216.0
    return out


def _extract_book_fields(attrs: dict) -> dict:
    """도서 attributes 추출. 값은 [{"value":...}] 리스트 형식."""
    out = {}
    if not isinstance(attrs, dict):
        return out
    for k_src, k_dst in _BOOK_ATTR_MAP:
        raw = attrs.get(k_src)
        if not raw:
            continue
        vals = []
        if isinstance(raw, list):
            for it in raw:
                v = it.get("value") if isinstance(it, dict) else it
                if v not in (None, ""):
                    vals.append(str(v).strip())
        elif isinstance(raw, dict):
            v = raw.get("value")
            if v not in (None, ""):
                vals.append(str(v).strip())
        else:
            vals.append(str(raw).strip())
        if not vals:
            continue
        if k_dst == "book_authors":
            out[k_dst] = [_norm_author(v) for v in vals]
        else:
            out[k_dst] = vals[0] if len(vals) == 1 else vals
    return out


def _extract_from_attributes(attrs: dict) -> dict:
    """attributes 에서 무게/용량/서빙수/단위 카운트/맛/사이즈 등 핵심 필드 정규화."""
    if not isinstance(attrs, dict):
        return {}
    out = {}

    # 무게 — 여러 키에 분산. 우선순위: item_weight > item_display_weight > item_package_weight
    iw = _first_attr_value(attrs, "item_weight")
    if isinstance(iw, dict):
        g = _to_grams(iw.get("value"), iw.get("unit"))
        if g is not None:
            out["item_weight_g"] = g

    idw = _first_attr_value(attrs, "item_display_weight")
    if isinstance(idw, dict):
        g = _to_grams(idw.get("value"), idw.get("unit"))
        if g is not None:
            out["item_display_weight_g"] = g

    ipw = _first_attr_value(attrs, "item_package_weight")
    if isinstance(ipw, dict):
        g = _to_grams(ipw.get("value"), ipw.get("unit"))
        if g is not None:
            out["package_weight_g"] = g

    # net_content — Amazon 라벨의 순중량/순부피. 단위가 무게/부피 둘 다 가능.
    nc = _first_attr_value(attrs, "net_content_volume")
    if isinstance(nc, dict):
        unit = (nc.get("unit") or "").lower()
        val = nc.get("value")
        out["net_content_value"] = val
        out["net_content_unit"] = nc.get("unit")
        # 단위가 무게면 g로, 부피면 ml로 정규화
        g = _to_grams(val, unit)
        if g is not None:
            out["net_content_g"] = g
        ml = _to_ml(val, unit)
        if ml is not None:
            out["net_content_ml"] = ml

    # item_volume — 부피 직접
    iv = _first_attr_value(attrs, "item_volume")
    if isinstance(iv, dict):
        ml = _to_ml(iv.get("value"), iv.get("unit"))
        if ml is not None:
            out["item_volume_ml"] = ml

    # capacity — 용기/카라페 용량 (커피메이커·물병·텀블러 등). Amazon이 용량을 여기 담음.
    cap = _first_attr_value(attrs, "capacity")
    if isinstance(cap, dict):
        ml = _to_ml(cap.get("value"), cap.get("unit"))
        if ml is not None:
            out["capacity_ml"] = ml

    # 모델번호/품번 — GTIN 없는 상품의 쿠팡 modelNo(품번) 소스 (식별번호 정책 2026-08-01)
    for _k in ("model_number", "part_number"):
        _v = _first_attr_value(attrs, _k)
        if isinstance(_v, dict):
            _v = _v.get("value")
        if isinstance(_v, str) and _v.strip():
            out[_k] = _v.strip()

    # 서빙 수
    tspc = _first_attr_value(attrs, "total_servings_per_container")
    if isinstance(tspc, dict) and tspc.get("value") is not None:
        try:
            out["total_servings"] = int(float(tspc["value"]))
        except (TypeError, ValueError):
            pass

    nos = _first_attr_value(attrs, "number_of_servings")
    if isinstance(nos, dict) and nos.get("value") is not None:
        try:
            out["number_of_servings"] = int(float(nos["value"]))
        except (TypeError, ValueError):
            pass

    # 단위 카운트 (e.g. "80 Ounce", "60 Capsule")
    uc = _first_attr_value(attrs, "unit_count")
    if isinstance(uc, dict):
        out["unit_count_value"] = uc.get("value")
        ut = uc.get("type")
        if isinstance(ut, dict):
            out["unit_count_unit"] = ut.get("value")
        elif isinstance(ut, str):
            out["unit_count_unit"] = ut

    # 개수 (Pack of N)
    ni = _first_attr_value(attrs, "number_of_items")
    if isinstance(ni, dict) and ni.get("value") is not None:
        try:
            out["number_of_items"] = int(float(ni["value"]))
        except (TypeError, ValueError):
            pass

    # 맛 / 사이즈 (attributes 측 — summaries 와 별개로 채워질 수 있음)
    fl = _first_attr_value(attrs, "flavor")
    if isinstance(fl, dict) and fl.get("value"):
        out["flavor_attr"] = fl["value"]

    sz = _first_attr_value(attrs, "size")
    if isinstance(sz, dict) and sz.get("value"):
        out["size_attr"] = sz["value"]

    out.update(_extract_age_fields(attrs))

    # bullet_points / description
    bullets = attrs.get("bullet_point") or []
    if isinstance(bullets, list):
        bp = [b.get("value") for b in bullets if isinstance(b, dict) and b.get("value")]
        if bp:
            out["bullet_points"] = bp

    descs = attrs.get("product_description") or []
    if isinstance(descs, list) and descs:
        d0 = descs[0]
        if isinstance(d0, dict) and d0.get("value"):
            out["description_en"] = d0["value"]

    # 보충제 전용
    for key, dst in [
        ("supplement_size_description", "supplement_size"),
        ("dosage_form", "dosage_form"),
        ("serving_size", "serving_size"),
        ("item_form", "item_form"),
        ("material", "material"),
    ]:
        v = _first_attr_value(attrs, key)
        if isinstance(v, dict) and v.get("value"):
            out[dst] = v["value"]

    out.update(_extract_book_fields(attrs))
    return out


def _extract_from_relationships(relationships: list) -> dict:
    """variation 정보. relationships[0].relationships[*] 에서 type=VARIATION 찾기."""
    if not relationships:
        return {}
    for entry in relationships:
        if not isinstance(entry, dict):
            continue
        rels = entry.get("relationships") or []
        for r in rels:
            if not isinstance(r, dict):
                continue
            if r.get("type") != "VARIATION":
                continue
            parents = r.get("parentAsins") or []
            theme = r.get("variationTheme") or {}
            out = {}
            if parents:
                out["parent_asin"] = parents[0]
            if isinstance(theme, dict):
                if theme.get("theme"):
                    out["variation_theme"] = theme["theme"]
                if theme.get("attributes"):
                    out["variation_dimensions"] = list(theme["attributes"])
            if out:
                return out
    return {}


def _extract_images(image_sets: list, max_images: int = 15) -> list:
    """SP-API images: variant=MAIN/PT01 등 여러 set. MAIN 우선, 같은 ID 중 최대 면적."""
    import re
    if not image_sets:
        return []
    main_set = image_sets[0]
    for s in image_sets:
        if isinstance(s, dict) and s.get("variant") == "MAIN":
            main_set = s
            break
    if not isinstance(main_set, dict):
        return []
    raw = main_set.get("images", [])
    if not isinstance(raw, list):
        return []
    pat_id = re.compile(r"/I/([A-Za-z0-9+_%-]+)\.")
    best_by_id: dict[str, tuple[int, str]] = {}
    main_ids: set = set()
    for img in raw:
        if not isinstance(img, dict):
            continue
        url = img.get("link") or ""
        if not url:
            continue
        area = (img.get("width") or 0) * (img.get("height") or 0)
        m = pat_id.search(url)
        img_id = m.group(1) if m else url
        # ★2026-08-03: variant 는 바깥 set 이 아니라 개별 이미지에 붙는다.
        #   기존 코드는 바깥에서 MAIN 을 찾아 늘 image_sets[0] 을 쓰고,
        #   내부는 면적 내림차순으로만 정렬해 "가장 큰 이미지"가 대표가 됐다.
        #   실측 결과 우리 첫 장이 아마존 MAIN 과 다른 경우가 29%.
        if img.get("variant") == "MAIN":
            main_ids.add(img_id)
        if img_id not in best_by_id or area > best_by_id[img_id][0]:
            best_by_id[img_id] = (area, url)
    # MAIN 을 항상 첫 장으로, 나머지는 기존대로 면적 내림차순
    mains = [(a, u) for i, (a, u) in best_by_id.items() if i in main_ids]
    rest = [(a, u) for i, (a, u) in best_by_id.items() if i not in main_ids]
    mains.sort(key=lambda x: -x[0])
    rest.sort(key=lambda x: -x[0])
    return [url for _, url in (mains + rest)[:max_images]]


def _extract_sales_rank(sales_ranks: list) -> Optional[int]:
    if not sales_ranks:
        return None
    for entry in sales_ranks:
        if not isinstance(entry, dict):
            continue
        ranks = entry.get("classificationRanks") or entry.get("displayGroupRanks") or []
        for r in ranks:
            if isinstance(r, dict) and r.get("rank") is not None:
                try:
                    return int(r["rank"])
                except (TypeError, ValueError):
                    pass
    return None


def normalize_catalog_item(asin: str, item: dict, marketplace: str = "US") -> dict:
    """SP-API CatalogItems payload → 정규화 표준 dict.

    반환 키:
      asin, marketplace, fetched_at,
      title_en, brand, manufacturer, model_number, part_number, color,
      size_label, flavor_label(=flavor_attr), style, release_date, package_quantity,
      browse_classification, website_display_group_name,
      item_dimensions: {length_cm, width_cm, height_cm, weight_g},
      package_dimensions: {length_cm, width_cm, height_cm, weight_g},
      item_weight_g, item_display_weight_g, package_weight_g,
      net_content_value, net_content_unit, net_content_g, net_content_ml, item_volume_ml,
      total_servings, number_of_servings, unit_count_value, unit_count_unit,
      number_of_items, flavor_attr, size_attr, supplement_size, dosage_form,
      serving_size, item_form, material,
      bullet_points: [str], description_en,
      images: [url],
      parent_asin, variation_theme, variation_dimensions: [str],
      sales_rank
    """
    if not isinstance(item, dict):
        return {"asin": asin, "marketplace": marketplace}
    out: dict = {
        "asin": asin,
        "marketplace": marketplace,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    out.update(_extract_from_summaries(item.get("summaries") or []))

    # ★2026-08-11: 상품명은 attributes.item_name 이 원문이다.
    #   summaries[0].itemName 은 아마존이 가공한 "표시용" 이름이라 200자 안팎에서 잘리고,
    #   카탈로그 통합 시 대표 변형의 이름으로 대체되기도 한다.
    #   실측(표본 400): 저장분의 32%가 원문과 불일치, 길이 분포가 197~200자에 몰림.
    #   원문을 우선하고 itemName 은 폴백으로만 쓴다.
    _raw_name = _first_attr_value(item.get("attributes") or {}, "item_name")
    if isinstance(_raw_name, dict):
        _raw_name = _raw_name.get("value")
    if isinstance(_raw_name, str) and _raw_name.strip():
        out["title_en"] = _raw_name.strip()
    # productType 추출 (도서/미디어 필터링용)
    pts = item.get("productTypes") or []
    if pts and isinstance(pts, list):
        first = pts[0] if isinstance(pts[0], dict) else None
        if first and first.get("productType"):
            out["product_type"] = first["productType"]
    out.update(_extract_dimensions(item.get("dimensions") or []))
    out.update(_extract_from_attributes(item.get("attributes") or {}))
    out.update(_extract_from_relationships(item.get("relationships") or []))
    images = _extract_images(item.get("images") or [])
    if images:
        out["images"] = images
    sr = _extract_sales_rank(item.get("salesRanks") or [])
    if sr is not None:
        out["sales_rank"] = sr
    # ★2026-08-04: identifiers(EAN/UPC/GTIN) 추출 — 기존엔 버려져 identifiers_json 이 NULL 이었다.
    #   GTIN 이 있으면 쿠팡이 modelNo 를 요구하지 않아 "상세설명참조" 노출제한을 피할 수 있다.
    #   (실측 BISSELL 3353: SP-API 는 EAN/UPC 를 주는데 DB 에 저장 안 됨)
    _ids = []
    for _grp in (item.get("identifiers") or []):
        if not isinstance(_grp, dict):
            continue
        for _x in (_grp.get("identifiers") or []):
            if isinstance(_x, dict) and _x.get("identifier"):
                _ids.append({"type": (_x.get("identifierType") or "").upper(),
                             "value": str(_x["identifier"]).strip()})
    if _ids:
        out["identifiers"] = _ids
    return out


# ── SP-API 호출 (rate limited) ────────────────────────────
_last_call_ts = 0.0


def _rate_limit_wait():
    global _last_call_ts
    now = time.monotonic()
    elapsed = now - _last_call_ts
    if elapsed < _SP_API_INTERVAL_SEC:
        time.sleep(_SP_API_INTERVAL_SEC - elapsed)
    _last_call_ts = time.monotonic()


_THROTTLE_MARKERS = ("429", "quotaexceeded", "throttl", "toomanyrequests", "too many requests")


def _is_throttle(exc) -> bool:
    """SP-API 스로틀(429) 예외 판별 — 클래스명/메시지 양쪽 검사 (SDK 예외 직접 import 불필요)."""
    name = type(exc).__name__.lower()
    s = str(exc).lower()
    return "throttl" in name or any(m in s for m in _THROTTLE_MARKERS)


def sp_api_retry(fn, *, retries: int = 3, base_backoff: float = 2.0, label: str = ""):
    """SP-API 호출 래퍼: 429/스로틀이면 지수 백오프(2/4/8s) 재시도, 그 외 예외는 즉시 raise.

    2026-06-03: getItemOffers 0.5 RPS 한도 초과로 스로틀당한 호출이 백오프 없이
    조용히 실패 → 가격 누락 사고 방지. 카탈로그/가격 양쪽 적용.
    """
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if _is_throttle(e) and attempt < retries:
                wait = base_backoff * (2 ** attempt)
                logger.warning(
                    f"[sp-api retry] {label or 'call'} 스로틀(429) — {wait:.0f}s 후 재시도 {attempt + 1}/{retries}"
                )
                time.sleep(wait)
                continue
            raise


def _call_catalog_items(asin: str, marketplace: str = "US") -> Optional[dict]:
    """단일 SP-API 호출. 모든 includedData 포함."""
    try:
        from sp_api.api import CatalogItems
        from sp_api.base import Marketplaces
        from backend.dropshipping.services.amazon_sp_api_service import get_credentials
    except ImportError as e:
        logger.warning(f"sp_api 모듈 import 실패: {e}")
        return None

    mp_obj = getattr(Marketplaces, marketplace, Marketplaces.US)
    mp_id = {"US": "ATVPDKIKX0DER", "CA": "A2EUQ1WTGCTBG2", "MX": "A1AM78C64UM0Y8"}.get(
        marketplace, "ATVPDKIKX0DER"
    )

    _rate_limit_wait()
    try:
        creds = get_credentials()
        catalog = CatalogItems(credentials=creds, marketplace=mp_obj, version="2022-04-01")
        resp = sp_api_retry(
            lambda: catalog.get_catalog_item(
                asin=asin,
                includedData=[
                    "summaries", "attributes", "images", "dimensions",
                    "productTypes", "identifiers", "relationships", "salesRanks",
                ],
                marketplaceIds=[mp_id],
            ),
            label=f"catalog {asin}",
        )
        return resp.payload or {}
    except Exception as e:
        logger.warning(f"[sp_api_facts] CatalogItems {asin} 실패: {e}")
        return None


# ── 메인 진입점 (캐시 + DB 저장) ────────────────────────
def fetch_full_catalog_facts(
    asin: str,
    marketplace: str = "US",
    force: bool = False,
    persist: bool = True,
) -> Optional[dict]:
    """ASIN → 정규화 facts dict.

    동작:
      1. force=False 이고 products.sp_api_facts_at 가 7일 이내면 DB 캐시 반환
      2. SP-API 호출 → 정규화
      3. persist=True 면 products 테이블 UPDATE (parent_asin / sp_api_facts_json /
         sp_api_facts_at + 보강 가능한 기존 컬럼: weight_g, brand, manufacturer 등)
      4. 정규화 dict 반환

    반환: facts dict 또는 None (호출 실패).
    """
    if not asin:
        return None

    asin = asin.strip().upper()

    # 1) 캐시 조회
    if not force and persist:
        cached = _load_cached_facts(asin)
        if cached:
            return cached

    # 2) SP-API 호출
    item = _call_catalog_items(asin, marketplace)
    if item is None:
        return None
    facts = normalize_catalog_item(asin, item, marketplace)

    # 3) DB 저장
    if persist:
        try:
            _persist_facts(asin, facts, raw=item)
        except Exception as e:
            logger.warning(f"[sp_api_facts] persist 실패 {asin}: {e}")

    return facts


def fetch_facts_with_raw(asin: str, marketplace: str = "US"):
    """facts 정규화 dict + raw catalog item 둘 다 반환 (persist 안 함).

    fetch_and_insert_children 같이 INSERT 후 _persist_facts(asin, facts, raw)로
    sp_*/sp_raw_json 까지 저장하려는 호출측용 — SP-API 1회 호출로 raw 도 확보.
    반환: (facts, item) 또는 (None, None).
    """
    if not asin:
        return None, None
    item = _call_catalog_items(asin.strip().upper(), marketplace)
    if item is None:
        return None, None
    return normalize_catalog_item(asin.strip().upper(), item, marketplace), item


def _load_cached_facts(asin: str) -> Optional[dict]:
    """products 테이블에서 캐시된 facts 로드. TTL 초과 시 None."""
    try:
        from backend.purchase.database import get_db
    except ImportError:
        return None
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT sp_api_facts_json, sp_api_facts_at FROM products WHERE asin=? LIMIT 1",
                (asin,),
            ).fetchone()
    except Exception:
        return None
    if not row or not row["sp_api_facts_json"] or not row["sp_api_facts_at"]:
        return None
    try:
        ts = datetime.fromisoformat(row["sp_api_facts_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    age = datetime.now(timezone.utc) - ts
    if age > timedelta(days=_CACHE_TTL_DAYS):
        return None
    try:
        return json.loads(row["sp_api_facts_json"])
    except (json.JSONDecodeError, TypeError):
        return None


def _persist_facts(asin: str, facts: dict, raw: dict = None) -> None:
    """products 테이블 UPDATE. asin 매칭되는 모든 행 갱신.

    파생 컬럼도 같이 채움:
      - parent_asin
      - weight_g (item_weight_g 우선, 없으면 item_display_weight_g 또는 net_content_g)
      - brand (없으면 facts.brand)
      - manufacturer (없으면 facts.manufacturer)
      - description_en (없으면 facts.description_en — bullet_points 합성)
      - images_json (없으면 facts.images)
    """
    try:
        from backend.purchase.database import get_db
    except ImportError:
        return

    weight_g = facts.get("item_weight_g") or facts.get("item_display_weight_g") or facts.get("net_content_g")
    parent_asin = facts.get("parent_asin")
    brand = facts.get("brand")
    manufacturer = facts.get("manufacturer")

    # description_en 생성: facts.description_en 우선, 없으면 bullet_points 합성
    description_en = facts.get("description_en")
    if not description_en and facts.get("bullet_points"):
        description_en = "\n".join(f"• {b}" for b in facts["bullet_points"])

    images = facts.get("images") or []
    images_json = json.dumps(images, ensure_ascii=False) if images else None

    facts_json = json.dumps(facts, ensure_ascii=False)
    facts_at = facts.get("fetched_at") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_db() as conn:
        # 기존 컬럼은 비어있을 때만 채움 (덮어쓰기 방지)
        conn.execute(
            """UPDATE products SET
                  sp_api_facts_json = ?,
                  sp_api_facts_at = ?,
                  parent_asin = COALESCE(parent_asin, ?),
                  weight_g = COALESCE(weight_g, ?),
                  brand = COALESCE(NULLIF(brand, ''), ?),
                  description_en = COALESCE(NULLIF(description_en, ''), ?),
                  images_json = COALESCE(NULLIF(images_json, ''), NULLIF(images_json, '[]'), ?)
               WHERE asin = ?""",
            (facts_json, facts_at, parent_asin, weight_g, brand, description_en,
             images_json, asin),
        )

        # ★SP facts 전 필드를 sp_* 전용 컬럼에 미러(2026-06-29) — Amazon 권위값이라 매 fetch 덮어씀.
        _SCALAR = {
            "color": "sp_color", "manufacturer": "sp_manufacturer", "model_number": "sp_model_number",
            "part_number": "sp_part_number", "size_label": "sp_size_label", "style": "sp_style",
            "material": "sp_material", "item_form": "sp_item_form", "flavor_attr": "sp_flavor",
            "size_attr": "sp_size_attr", "unit_count_unit": "sp_unit_count_unit",
            "net_content_unit": "sp_net_content_unit", "variation_theme": "sp_variation_theme",
            "browse_classification": "sp_browse_classification",
            "website_display_group_name": "sp_website_display_group", "product_type": "sp_product_type",
            "release_date": "sp_release_date", "dosage_form": "sp_dosage_form",
            "serving_size": "sp_serving_size", "supplement_size": "sp_supplement_size",
            "title_en": "sp_title_en", "brand": "sp_brand",
            "age_range_desc": "sp_age_range_desc",
            "target_audience": "sp_target_audience",
            "cpsia_warning": "sp_cpsia_warning",
            "safety_warning_text": "sp_safety_warning",
        }
        _NUM = {
            "number_of_items": "sp_number_of_items", "total_servings": "sp_total_servings",
            "number_of_servings": "sp_number_of_servings", "package_quantity": "sp_package_quantity",
            "sales_rank": "sp_sales_rank", "unit_count_value": "sp_unit_count_value",
            "net_content_value": "sp_net_content_value", "capacity_ml": "sp_capacity_ml",
            "net_content_g": "sp_net_content_g", "net_content_ml": "sp_net_content_ml",
            "item_volume_ml": "sp_item_volume_ml", "item_weight_g": "sp_item_weight_g",
            "item_display_weight_g": "sp_item_display_weight_g", "package_weight_g": "sp_package_weight_g",
            "age_min_months": "sp_age_min_months",
            "age_max_months": "sp_age_max_months",
            "kc_min_age_months": "sp_kc_min_age_months",
        }
        _JSONC = {
    "identifiers": "identifiers_json",   # ★2026-08-04 GTIN 저장"variation_dimensions": "sp_variation_dims_json",
                  "bullet_points": "sp_bullet_points_json", "images": "sp_images_json"}
        _DIM = {
            ("item_dimensions", "length_cm"): "sp_item_length_cm", ("item_dimensions", "width_cm"): "sp_item_width_cm",
            ("item_dimensions", "height_cm"): "sp_item_height_cm", ("item_dimensions", "weight_g"): "sp_item_dim_weight_g",
            ("package_dimensions", "length_cm"): "sp_pkg_length_cm", ("package_dimensions", "width_cm"): "sp_pkg_width_cm",
            ("package_dimensions", "height_cm"): "sp_pkg_height_cm", ("package_dimensions", "weight_g"): "sp_pkg_weight_g",
        }
        _sets = {}
        for _k, _c in _SCALAR.items():
            if facts.get(_k) not in (None, ""):
                _sets[_c] = str(facts[_k])[:500]
        for _k, _c in _NUM.items():
            _v = facts.get(_k)
            if isinstance(_v, (int, float)):
                _sets[_c] = _v
            elif isinstance(_v, str):
                try: _sets[_c] = float(_v)
                except ValueError: pass
        for _k, _c in _JSONC.items():
            if facts.get(_k):
                _sets[_c] = json.dumps(facts[_k], ensure_ascii=False)
        for (_dk, _ax), _c in _DIM.items():
            _d = facts.get(_dk) or {}
            if isinstance(_d, dict) and isinstance(_d.get(_ax), (int, float)):
                _sets[_c] = _d[_ax]
        if raw is not None:
            try:
                _sets["sp_raw_json"] = json.dumps(raw, ensure_ascii=False)  # ★원본 catalog item 통째 보관
            except Exception:
                pass
        if _sets:
            try:
                _cols = ", ".join(f"{_c}=?" for _c in _sets)
                conn.execute(f"UPDATE products SET {_cols} WHERE asin = ?", list(_sets.values()) + [asin])
            except Exception as _e:
                logger.warning(f"[sp-facts] sp_* 컬럼 미러 실패 {asin}: {_e}")


# ── 호환 래퍼 (기존 호출처) ─────────────────────────────────
def get_facts_for_promote(asin: str) -> dict:
    """sourcing_promote._enrich_from_sp_api 대체.

    반환 (기존 형식 호환):
      {title, brand, description, bullet_points, images}
    """
    facts = fetch_full_catalog_facts(asin)
    if not facts:
        return {}
    return {
        "title": facts.get("title_en", ""),
        "brand": facts.get("brand", ""),
        "manufacturer": facts.get("manufacturer", ""),
        "description": facts.get("description_en", ""),
        "bullet_points": facts.get("bullet_points") or [],
        "images": facts.get("images") or [],
    }


def get_image_urls(asin: str, max_images: int = 15) -> list:
    """image_downloader.fetch_amazon_images_sp_api 대체.

    캐시 우선 → 없으면 SP-API 호출.
    """
    facts = fetch_full_catalog_facts(asin)
    if not facts:
        return []
    return (facts.get("images") or [])[:max_images]


def get_strict_facts(asin: str) -> dict:
    """coupang_attributes._fetch_sp_api_facts 대체.

    Tier 2 strict 추출 시 Gemini 프롬프트에 dump 할 facts. 정규화 dict 그대로
    반환 — 기존 free-form dict 보다 키 이름이 더 명시적이라 AI 가 더 잘 해석.
    """
    facts = fetch_full_catalog_facts(asin)
    return facts or {}
