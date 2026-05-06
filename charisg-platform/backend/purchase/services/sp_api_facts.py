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
    if u in ("fl_oz", "fl oz", "fluid_ounce", "fluid ounces"):
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
    for img in raw:
        if not isinstance(img, dict):
            continue
        url = img.get("link") or ""
        if not url:
            continue
        area = (img.get("width") or 0) * (img.get("height") or 0)
        m = pat_id.search(url)
        img_id = m.group(1) if m else url
        if img_id not in best_by_id or area > best_by_id[img_id][0]:
            best_by_id[img_id] = (area, url)
    sorted_imgs = sorted(best_by_id.values(), key=lambda x: -x[0])
    return [url for _, url in sorted_imgs[:max_images]]


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
        resp = catalog.get_catalog_item(
            asin=asin,
            includedData=[
                "summaries", "attributes", "images", "dimensions",
                "productTypes", "identifiers", "relationships", "salesRanks",
            ],
            marketplaceIds=[mp_id],
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
            _persist_facts(asin, facts)
        except Exception as e:
            logger.warning(f"[sp_api_facts] persist 실패 {asin}: {e}")

    return facts


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


def _persist_facts(asin: str, facts: dict) -> None:
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
