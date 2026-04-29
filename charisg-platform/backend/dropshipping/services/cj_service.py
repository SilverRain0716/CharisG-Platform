"""
cj_service.py — CJ Dropshipping API 서비스
키워드 기반 상품 검색, 배송비 조회, 마진 계산
cj_crawler.py의 핵심 로직을 백엔드 서비스로 분리

사용처:
  - /api/trends/ai-sourcing → 트렌드 키워드 → CJ 검색
  - /api/crawl-jobs → CJ 크롤잡 실행
"""
import logging
import os
import time
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

from backend.dropshipping.services.amazon_fee_service import calc_real_margin, get_amazon_category

# CJ API 설정
CJ_API_KEY = os.environ.get("CJ_API_KEY", "")
CJ_API_BASE = "https://developers.cjdropshipping.com/api2.0/v1"

# 필터 기본값
DEFAULT_PRICE_MIN = 0.0    # 소싱가 하한 제거 (판매가 $15+ 하드필터가 대체)
DEFAULT_PRICE_MAX = 9999.0  # 소싱가 상한 제거
DEFAULT_MARGIN_MIN = 25.0  # Amazon Referral Fee 반영 후 실질 마진 기준

from backend.dropshipping.database import get_db


def _extract_warehouse(raw: dict) -> tuple[bool, str | None]:
    """CJ API 응답에서 창고 국가 추출.

    Returns: (us_warehouse: bool, warehouse_country: str|None)
    """
    shipping_codes = raw.get("shippingCountryCodes") or []
    if not shipping_codes:
        shipping_codes = [
            str(w.get("countryCode", "")).upper()
            for w in raw.get("sourceWarehouse", [])
        ]
    else:
        shipping_codes = [str(c).upper() for c in shipping_codes]

    us_warehouse = "US" in shipping_codes
    cn_warehouse = "CN" in shipping_codes
    warehouse_country = "US" if us_warehouse else ("CN" if cn_warehouse else None)
    return us_warehouse, warehouse_country


def _update_warehouse_info(raw: dict, pid: str):
    """모든 상품의 창고 정보를 DB에 업데이트 (Hard Filter 통과 여부 무관)."""
    us_warehouse, warehouse_country = _extract_warehouse(raw)
    if not pid:
        return
    try:
        with get_db() as conn:
            conn.execute(
                """UPDATE collected_products
                   SET us_warehouse=?, warehouse_country=?
                   WHERE external_id=? AND source='cj'""",
                (int(us_warehouse), warehouse_country or "", pid),
            )
    except Exception:
        pass  # 존재하지 않는 상품이면 무시


def _log_filter_fail(pid: str, name: str, category: str,
                     source_price: float, calculated_price: float, reason: str):
    """Hard Filter 탈락 상품을 DB에 기록 (filter_fail_reason 포함)"""
    try:
        with get_db() as conn:
            # 이미 존재하면 사유만 업데이트
            existing = conn.execute(
                "SELECT id FROM collected_products WHERE external_id = ? AND source = 'cj'",
                (pid,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE collected_products SET hard_filter_pass=0, filter_fail_reason=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (reason, existing["id"]),
                )
            else:
                url = f"https://app.cjdropshipping.com/product-detail.html?pid={pid}"
                conn.execute(
                    """INSERT OR IGNORE INTO collected_products
                       (source, business_model, external_id, url, product_name, category,
                        source_price, source_currency, calculated_price,
                        hard_filter_pass, filter_fail_reason, status,
                        processing_status, collected_at)
                       VALUES ('cj','dropship',?,?,?,?,?,'USD',?,0,?,'filtered','raw',CURRENT_TIMESTAMP)""",
                    (pid, url, name, category, source_price, calculated_price, reason),
                )
    except Exception as e:
        logger.debug(f"탈락 사유 기록 실패 ({pid}): {e}")


# 토큰 캐시
_cached_token: Optional[str] = None
_token_expires: float = 0


def _get_token() -> Optional[str]:
    """CJ API 토큰 발급 (12시간 캐시)"""
    global _cached_token, _token_expires

    if _cached_token and time.time() < _token_expires:
        return _cached_token

    if not CJ_API_KEY:
        logger.error("CJ_API_KEY 미설정")
        return None

    try:
        resp = requests.post(
            f"{CJ_API_BASE}/authentication/getAccessToken",
            json={"apiKey": CJ_API_KEY},
            timeout=15,
        )
        data = resp.json()
        if data.get("result"):
            _cached_token = data["data"]["accessToken"]
            _token_expires = time.time() + 11 * 3600  # 11시간 (여유)
            logger.info("✅ CJ 토큰 발급 성공")
            return _cached_token
        else:
            logger.error(f"CJ 토큰 실패: {data.get('message')}")
            return None
    except Exception as e:
        logger.error(f"CJ 토큰 오류: {e}")
        return None


def search_products(
    keyword: str,
    page: int = 1,
    page_size: int = 50,
    price_min: float = DEFAULT_PRICE_MIN,
    price_max: float = DEFAULT_PRICE_MAX,
    margin_min: float = DEFAULT_MARGIN_MIN,
) -> list[dict]:
    """
    CJ 키워드 검색 → 필터링된 상품 리스트 반환.

    Returns: [{"pid", "name", "sell_price", "suggest_price", "ship_cost",
               "margin_pct", "inventory", "us_warehouse", "image_url", "url"}, ...]
    """
    token = _get_token()
    if not token:
        return []

    try:
        time.sleep(0.5)  # CJ API rate limit
        resp = requests.get(
            f"{CJ_API_BASE}/product/list",
            headers={"CJ-Access-Token": token},
            params={
                "pageNum": page,
                "pageSize": page_size,
                "productNameEn": keyword,
            },
            timeout=20,
        )
        data = resp.json()
        if not data.get("result"):
            logger.warning(f"CJ 검색 실패 ({keyword}): {data.get('message')}")
            return []

        raw_list = data.get("data", {}).get("list", [])
    except Exception as e:
        logger.error(f"CJ 검색 오류 ({keyword}): {e}")
        return []

    results = []
    for raw in raw_list:
        item = _parse_product(raw, keyword, token, price_min, price_max, margin_min)
        if item and _is_relevant(item["name"], keyword):
            results.append(item)

    return results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 카테고리 트리 기반 전수 수집
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_category_tree() -> list[dict]:
    """CJ 카테고리 트리 조회 → leaf 카테고리(Level 3) 리스트 반환.

    Returns: [{"id": categoryId, "name": full_path, "level1": ..., "level2": ..., "level3": ...}]
    """
    token = _get_token()
    if not token:
        return []

    try:
        resp = requests.get(
            f"{CJ_API_BASE}/product/getCategory",
            headers={"CJ-Access-Token": token},
            timeout=15,
        )
        data = resp.json()
        if not data.get("result"):
            logger.error(f"CJ 카테고리 조회 실패: {data.get('message')}")
            return []
    except Exception as e:
        logger.error(f"CJ 카테고리 오류: {e}")
        return []

    leaves = []
    for cat1 in data.get("data", []):
        l1_name = cat1.get("categoryFirstName", "")
        for cat2 in cat1.get("categoryFirstList", []):
            l2_name = cat2.get("categorySecondName", "")
            for leaf in cat2.get("categorySecondList", []):
                leaves.append({
                    "id": leaf.get("categoryId", ""),
                    "name": f"{l1_name} > {l2_name} > {leaf.get('categoryName', '')}",
                    "level1": l1_name,
                    "level2": l2_name,
                    "level3": leaf.get("categoryName", ""),
                })
    logger.info(f"CJ 카테고리 트리: leaf {len(leaves)}개")
    return leaves


# 소싱 대상에서 제외할 Level 1 카테고리 (스펙 Hard Filter 8번)
EXCLUDED_LEVEL1 = {
    "Women's Clothing",
    "Men's Clothing",
    "Kids & Baby Clothing",
    "Shoes",
    "Health, Beauty & Hair",  # Health 관련
    "Wedding & Events",        # 의류 계열
}


def search_by_category(
    category_id: str,
    page: int = 1,
    page_size: int = 50,
    country_code: str = "US",
) -> tuple[list[dict], int]:
    """단일 카테고리의 한 페이지 조회.

    Returns: (raw_items, total_count)
    """
    token = _get_token()
    if not token:
        return [], 0

    try:
        time.sleep(0.5)
        resp = requests.get(
            f"{CJ_API_BASE}/product/list",
            headers={"CJ-Access-Token": token},
            params={
                "categoryId": category_id,
                "pageNum": page,
                "pageSize": page_size,
                "countryCode": country_code,
            },
            timeout=20,
        )
        data = resp.json()
        if not data.get("result"):
            return [], 0
        raw = data.get("data", {}) or {}
        return raw.get("list", []) or [], int(raw.get("total", 0))
    except Exception as e:
        logger.error(f"CJ 카테고리 조회 오류 ({category_id}): {e}")
        return [], 0


def collect_full_catalog(
    progress_cb=None,
    max_pages_per_category: int = 10,
    page_size: int = 50,
    skip_excluded: bool = True,
) -> dict:
    """CJ US 창고 전체 카탈로그 수집 → collected_products 저장.

    스펙: CJ 38K → Collected 6.2K → Hard Filter 335
    단계:
      1) 카테고리 트리 조회 (~539 leaf)
      2) 제외 카테고리 스킵 (Clothing/Health)
      3) 각 leaf 카테고리 페이지네이션 순회 (US 창고만)
      4) _parse_product 로 Hard Filter 적용 → pass인 것만 저장

    Args:
        progress_cb: callable(phase, current, total, message) — 진행률 콜백
        max_pages_per_category: 카테고리당 최대 페이지
        page_size: 페이지당 상품 수 (최대 50)
        skip_excluded: Clothing/Health 등 제외 카테고리 스킵 여부

    Returns:
        {"categories": int, "raw_collected": int, "filter_passed": int, "saved": int}
    """
    from backend.dropshipping.database import get_db

    token = _get_token()
    if not token:
        logger.error("CJ 토큰 발급 실패 — 전수 수집 중단")
        return {"categories": 0, "raw_collected": 0, "filter_passed": 0, "saved": 0}

    categories = get_category_tree()
    if not categories:
        logger.error("CJ 카테고리 트리 조회 실패")
        return {"categories": 0, "raw_collected": 0, "filter_passed": 0, "saved": 0}

    if skip_excluded:
        categories = [c for c in categories if c["level1"] not in EXCLUDED_LEVEL1]

    total_cats = len(categories)
    raw_collected = 0
    filter_passed = 0
    saved = 0

    if progress_cb:
        progress_cb("collect", 0, total_cats, f"카테고리 {total_cats}개 수집 시작")

    for idx, cat in enumerate(categories, 1):
        cat_id = cat["id"]
        cat_label = cat["name"]

        if progress_cb and idx % 10 == 0:
            progress_cb("collect", idx, total_cats, f"{cat_label[:40]}")

        for page in range(1, max_pages_per_category + 1):
            raw_list, total = search_by_category(cat_id, page=page, page_size=page_size)
            if not raw_list:
                break
            raw_collected += len(raw_list)

            for raw in raw_list:
                # ── 모든 상품의 창고 정보 먼저 업데이트 ──
                raw_pid = raw.get("pid", "")
                _update_warehouse_info(raw, raw_pid)

                item = _parse_product(
                    raw, cat["level3"], token,
                    DEFAULT_PRICE_MIN, DEFAULT_PRICE_MAX, DEFAULT_MARGIN_MIN,
                )
                if not item:
                    continue  # Hard Filter 탈락 (log_filter_fail에서 DB 저장됨)
                filter_passed += 1

                # DB 저장
                try:
                    with get_db() as conn:
                        existing = conn.execute(
                            "SELECT id FROM collected_products WHERE external_id=? AND source='cj'",
                            (item["pid"],),
                        ).fetchone()
                        if existing:
                            # listed/active 상품은 status 보호 (Amazon 등록 유지)
                            conn.execute(
                                """UPDATE collected_products
                                   SET product_name=?, calculated_price=?, source_price=?,
                                       shipping_cost=?, real_margin_pct=?, stock_quantity=?,
                                       weight_g=?, image_count=?, hard_filter_pass=1,
                                       filter_fail_reason=NULL,
                                       us_warehouse=?, warehouse_country=?,
                                       status = CASE WHEN status IN ('listed','active') THEN status ELSE 'collected' END,
                                       category=?, updated_at=CURRENT_TIMESTAMP
                                   WHERE id=?""",
                                (item["name"], item["suggest_price"], item["sell_price"],
                                 item["ship_cost"], item["margin_pct"], item["inventory"],
                                 item["weight_g"], item["image_count"],
                                 int(item["us_warehouse"]), item["warehouse_country"],
                                 cat["level3"], existing["id"]),
                            )
                        else:
                            conn.execute(
                                """INSERT INTO collected_products
                                   (source, business_model, external_id, url, product_name, category,
                                    source_price, source_currency, calculated_price, shipping_cost,
                                    real_margin_pct, stock_quantity, weight_g, image_count,
                                    hard_filter_pass, filter_fail_reason, status,
                                    us_warehouse, warehouse_country, processing_status, collected_at)
                                   VALUES ('cj','dropship',?,?,?,?,?,'USD',?,?,?,?,?,?,1,NULL,
                                           'collected',?,?,'raw',CURRENT_TIMESTAMP)""",
                                (item["pid"], item["url"], item["name"], cat["level3"],
                                 item["sell_price"], item["suggest_price"], item["ship_cost"],
                                 item["margin_pct"], item["inventory"], item["weight_g"],
                                 item["image_count"],
                                 int(item["us_warehouse"]), item["warehouse_country"]),
                            )
                            saved += 1
                except Exception as e:
                    logger.debug(f"CJ 저장 실패 ({item['pid']}): {e}")

            # 페이지 간 딜레이 (Rate Limit 대응)
            if len(raw_list) < page_size:
                break  # 마지막 페이지 도달
            time.sleep(0.8)

        # 카테고리 간 딜레이
        time.sleep(1.2)

    if progress_cb:
        progress_cb("collect", total_cats, total_cats,
                    f"수집 완료: raw={raw_collected}, pass={filter_passed}, saved={saved}")

    logger.info(f"CJ 전수 수집 완료: 카테고리 {total_cats}, raw {raw_collected}, "
                f"pass {filter_passed}, saved {saved}")
    return {
        "categories": total_cats,
        "raw_collected": raw_collected,
        "filter_passed": filter_passed,
        "saved": saved,
    }


def search_by_keywords(
    keywords: list[str],
    max_per_keyword: int = 20,
    price_min: float = DEFAULT_PRICE_MIN,
    price_max: float = DEFAULT_PRICE_MAX,
    margin_min: float = DEFAULT_MARGIN_MIN,
) -> list[dict]:
    """여러 키워드로 CJ 검색 → 중복 제거 후 반환"""
    seen_pids = set()
    all_results = []

    for kw in keywords:
        items = search_products(kw, page_size=max_per_keyword,
                                price_min=price_min, price_max=price_max,
                                margin_min=margin_min)
        for item in items:
            if item["pid"] not in seen_pids:
                seen_pids.add(item["pid"])
                all_results.append(item)
        time.sleep(1)  # 키워드 간 딜레이

    return all_results


def get_shipping_cost(
    product_sku: str,
    warehouse_country: str = "US",
    dest_country: str = "US",
) -> float:
    """창고→목적지 배송비 조회. 실패 시 기본값 반환."""
    from backend.dropshipping.services.marketplace_config import get_default_ship_cost
    default = get_default_ship_cost("US", warehouse_country)

    if not product_sku:
        return default

    token = _get_token()
    if not token:
        return default

    try:
        time.sleep(0.5)
        resp = requests.post(
            f"{CJ_API_BASE}/logistic/freightCalculate",
            headers={"CJ-Access-Token": token},
            json={
                "startCountryCode": warehouse_country,
                "endCountryCode": dest_country,
                "products": [{"skuId": product_sku, "quantity": 1}],
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("result") and data.get("data"):
            cheapest = min(data["data"], key=lambda x: float(x.get("logisticPrice", 999)))
            return float(cheapest.get("logisticPrice", default))
    except Exception:
        pass

    return default


def get_product_detail(pid: str) -> Optional[dict]:
    """CJ 상품 상세 조회 (이미지 포함)"""
    token = _get_token()
    if not token:
        return None

    try:
        time.sleep(0.5)
        resp = requests.get(
            f"{CJ_API_BASE}/product/query",
            headers={"CJ-Access-Token": token},
            params={"pid": pid},
            timeout=15,
        )
        data = resp.json()
        if data.get("result") and data.get("data"):
            return data["data"]
    except Exception as e:
        logger.error(f"CJ 상품 상세 오류 ({pid}): {e}")

    return None


def _parse_weight_g(raw: dict) -> float:
    """CJ API 응답에서 무게를 g 단위로 파싱."""
    weight = raw.get("productWeight")
    if not weight:
        return 0
    try:
        w = float(str(weight).replace(",", ""))
    except (ValueError, TypeError):
        return 0
    unit = str(raw.get("productUnit") or "g").lower().strip()
    if "kg" in unit:
        return w * 1000
    elif "lb" in unit or "lbs" in unit:
        return w * 453.6
    elif "oz" in unit:
        return w * 28.35
    return w


def _parse_product(
    raw: dict, keyword: str, token: str,
    price_min: float, price_max: float, margin_min: float,
) -> Optional[dict]:
    """CJ API 응답 → 정제된 상품 dict. Hard Filter 미달 시 None (탈락 사유 로깅)."""
    try:
        # 가격 파싱 (범위형 "28.63 -- 31.81" 처리)
        raw_price = str(raw.get("sellPrice") or "0")
        if "--" in raw_price:
            raw_price = raw_price.split("--")[0].strip()
        sell_price = float(raw_price)

        if not (price_min <= sell_price <= price_max):
            return None  # 소싱가 범위 밖 — Hard Filter 이전 단계

        suggest_price = float(raw.get("suggestSellingPrice") or 0)
        if suggest_price <= 0:
            suggest_price = round(sell_price * 2.5, 2)

        pid = raw.get("pid", "")
        sku = raw.get("productSku", "")
        product_name = raw.get("productNameEn", "")
        inventory = int(raw.get("inventory") or 0)

        # ═══ Hard Filter (탈락 사유 기록) ═══

        # [1] 창고 필터: US 또는 CN 중 하나 이상 필요 (US 우선)
        us_warehouse, warehouse_country = _extract_warehouse(raw)
        if not warehouse_country:
            shipping_codes = raw.get("shippingCountryCodes") or []
            _log_filter_fail(pid, product_name, keyword, sell_price, suggest_price,
                             f"no_us_cn_warehouse:{','.join(str(c) for c in shipping_codes) or 'empty'}")
            return None

        # [3] 재고 ≥ 10
        if inventory < 10:
            _log_filter_fail(pid, product_name, keyword, sell_price, suggest_price, f"low_stock:{inventory}")
            return None

        # [4] 판매가 $15+ (하한만 — 수수료/배송비 감안 최소 수익선)
        if suggest_price < 15:
            _log_filter_fail(pid, product_name, keyword, sell_price, suggest_price, f"price_too_low:${suggest_price:.0f}")
            return None

        # [5] 무게 ≤ 907g (2lbs)
        weight_g = _parse_weight_g(raw)
        if weight_g > 907:
            _log_filter_fail(pid, product_name, keyword, sell_price, suggest_price, f"overweight:{weight_g:.0f}g")
            return None

        # [6] 이미지 ≥ 3장
        image_set = raw.get("productImageSet") or []
        image_count = len(image_set) + (1 if raw.get("productImage") or raw.get("bigImage") else 0)
        if image_count < 3:
            _log_filter_fail(pid, product_name, keyword, sell_price, suggest_price, f"few_images:{image_count}")
            return None

        # 배송비
        ship_cost = get_shipping_cost(sku, warehouse_country=warehouse_country)

        # [2] 마진 계산 (Amazon Referral Fee 반영) ≥ 25%
        margin_pct = calc_real_margin(
            source_price=sell_price,
            ship_cost=ship_cost,
            sale_price=suggest_price,
            category=keyword,
            product_name=product_name,
        )

        if margin_pct < margin_min:
            _log_filter_fail(pid, product_name, keyword, sell_price, suggest_price, f"low_margin:{margin_pct:.1f}%")
            return None

        # ═══ Hard Filter 통과 ═══

        image_url = raw.get("productImage") or raw.get("bigImage") or ""

        return {
            "pid": pid,
            "name": product_name,
            "keyword": keyword,
            "sell_price": sell_price,
            "suggest_price": suggest_price,
            "ship_cost": ship_cost,
            "margin_pct": margin_pct,
            "inventory": inventory,
            "us_warehouse": us_warehouse,
            "warehouse_country": warehouse_country,
            "weight_g": weight_g,
            "image_count": image_count,
            "hard_filter_pass": True,
            "image_url": image_url,
            "url": f"https://app.cjdropshipping.com/product-detail.html?pid={pid}",
        }

    except Exception as e:
        logger.debug(f"CJ 상품 파싱 스킵 ({raw.get('pid')}): {e}")
        return None


def _is_relevant(product_name: str, keyword: str) -> bool:
    """상품명과 검색 키워드의 관련성 검사.

    2단어 연속 매칭 또는 핵심 명사 다수 매칭이 필요.
    예: "ice cube tray" → "silicone ice cube tray mold" ✅
        "resistance bands" → "sterling silver bands" ✗ (bands만 매칭)
    """
    name_lower = product_name.lower()
    kw_lower = keyword.lower()

    # 1차: 2단어 이상 연속 구문이 상품명에 포함되면 즉시 통과
    kw_parts = kw_lower.split()
    for i in range(len(kw_parts) - 1):
        bigram = f"{kw_parts[i]} {kw_parts[i + 1]}"
        if bigram in name_lower:
            return True

    # 2차: 핵심 명사 매칭 (수식어 제외)
    modifiers = {
        "for", "the", "and", "with", "set", "new", "in", "of", "a", "an", "to", "is",
        "mini", "portable", "electric", "rechargeable", "usb", "vintage", "aesthetic",
        "diy", "kit", "car", "home", "small", "large", "big", "travel", "magnetic",
    }
    kw_nouns = [w for w in kw_parts if w not in modifiers and len(w) >= 3]
    if not kw_nouns:
        return True

    matched = sum(1 for w in kw_nouns if w in name_lower)
    # 핵심 명사의 2/3 이상 일치 (최소 2개)
    threshold = max(2, int(len(kw_nouns) * 0.66))
    return matched >= threshold
