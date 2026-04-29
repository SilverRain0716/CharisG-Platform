"""
group_lister.py — Phase 3 multi-option 채널 등록.

핵심 함수:
  register_group_listings(parent_asin, channels) — 한 group 의 자동 분리 결과를 모든
    채널에 등록. 각 split 마다 listings_pa 1행 + listing_options N행.
  build_smartstore_payload(group, split, pricing_for_split) — 네이버 옵션 페이로드.
  build_coupang_payload(group, split, pricing_for_split) — 쿠팡 옵션 페이로드.

페이로드 차이:
  - 네이버: optionCombinations (base + delta). master detail/이미지 공유.
  - 쿠팡  : items 배열 (absolute price). 각 item 에 attributes·이미지 별도.

설계 결정:
  - 한 split 의 master = options[0] (auto_split 가 sales_rank 우선 정렬한 첫 번째)
  - listings_pa.product_id = master child 의 products.id
  - listings_pa.channel_product_id = 채널 API 응답의 sellerProductId/originProductNo
  - listing_options.channel_option_id = vendorItemId/channelProductNo
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 옵션 C-0 분석 — group → 기존 listings 매핑 + master 결정 ────────
def analyze_group_listings(parent_asin: str) -> dict:
    """group 의 children 별 listings_pa 보유 현황 + master 결정 + archive 후보.

    반환:
    {
      "parent_asin", "child_asins", "child_count",
      "smartstore": {
        "master_listing_id": int | None,
        "master_channel_product_id": str | None,
        "master_child_product_id": int | None,
        "master_child_asin": str | None,
        "subordinate_listings": [
          {listing_id, channel_product_id, child_product_id, child_asin, sales_rank}
        ],
        "new_options": [child_asin]  # 채널에 listing 없는 children (옵션 추가 시 신규 vendorItem)
      },
      "coupang": { 동일 },
      "strategy": "single_extend" | "multi_extend" | "options_add_only" | "no_action"
    }
    """
    from backend.purchase.database import get_db
    from backend.purchase.services.variation import load_group

    g = load_group(parent_asin)
    if not g:
        return {"error": f"group {parent_asin} 없음"}

    child_asins = g.get("child_asins") or []
    if not child_asins:
        return {"error": "child_asins 비어있음"}

    placeholders = ",".join("?" * len(child_asins))
    with get_db() as conn:
        # children 별 product 정보 + sales_rank
        rows = conn.execute(
            f"""SELECT p.id AS product_id, p.asin, p.cost_usd, p.sp_api_facts_json,
                       l.id AS listing_id, l.channel, l.channel_product_id, l.status, l.sale_krw
                FROM products p
                LEFT JOIN listings_pa l ON l.product_id = p.id AND l.status='listed'
                WHERE p.asin IN ({placeholders})""",
            child_asins,
        ).fetchall()

    # asin → product 정보
    products_by_asin: dict[str, dict] = {}
    listings_by_asin_channel: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        a = r["asin"]
        products_by_asin.setdefault(a, {
            "product_id": r["product_id"],
            "asin": a,
            "cost_usd": r["cost_usd"],
        })
        if r["listing_id"] and r["channel_product_id"]:
            listings_by_asin_channel.setdefault((a, r["channel"]), []).append({
                "listing_id": r["listing_id"],
                "channel_product_id": r["channel_product_id"],
                "sale_krw": r["sale_krw"],
            })

    # facts 에서 sales_rank 추출 (master 결정용)
    sales_rank_by_asin: dict[str, int] = {}
    for a, info in products_by_asin.items():
        # cached facts: products.sp_api_facts_json
        # rows 에 sp_api_facts_json 들어있음 — 첫 row 의 facts 사용
        for r in rows:
            if r["asin"] == a and r["sp_api_facts_json"]:
                try:
                    facts = json.loads(r["sp_api_facts_json"])
                    if facts.get("sales_rank"):
                        sales_rank_by_asin[a] = int(facts["sales_rank"])
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
                break

    out = {
        "parent_asin": parent_asin,
        "child_asins": child_asins,
        "child_count": len(child_asins),
        "our_products_count": len(products_by_asin),
    }

    for ch in ("smartstore", "coupang"):
        # 채널 별 listing 보유 children
        listed_asins = [a for (a, c) in listings_by_asin_channel.keys() if c == ch]
        not_listed_asins = [a for a in child_asins
                            if a in products_by_asin and a not in listed_asins]
        unknown_asins = [a for a in child_asins if a not in products_by_asin]

        # master 결정: listed children 중 sales_rank 가장 작음 (= 인기)
        master_asin = None
        if listed_asins:
            ranked = [(a, sales_rank_by_asin.get(a, 10**9)) for a in listed_asins]
            ranked.sort(key=lambda x: x[1])
            master_asin = ranked[0][0]

        master_info = None
        sub_listings = []
        if master_asin:
            ml = listings_by_asin_channel[(master_asin, ch)][0]
            master_info = {
                "child_asin": master_asin,
                "child_product_id": products_by_asin[master_asin]["product_id"],
                "listing_id": ml["listing_id"],
                "channel_product_id": ml["channel_product_id"],
                "sale_krw": ml["sale_krw"],
                "sales_rank": sales_rank_by_asin.get(master_asin),
            }
            for a in listed_asins:
                if a == master_asin:
                    continue
                ll = listings_by_asin_channel[(a, ch)][0]
                sub_listings.append({
                    "child_asin": a,
                    "child_product_id": products_by_asin[a]["product_id"],
                    "listing_id": ll["listing_id"],
                    "channel_product_id": ll["channel_product_id"],
                    "sale_krw": ll["sale_krw"],
                    "sales_rank": sales_rank_by_asin.get(a),
                })

        out[ch] = {
            "master": master_info,
            "subordinate_listings": sub_listings,           # archive 대상
            "new_options_no_listing": not_listed_asins,     # 옵션 추가만 (archive 없음)
            "unknown_children": unknown_asins,              # 우리 products 에 없음
        }

    # 전략 결정
    ss_sub = len(out["smartstore"]["subordinate_listings"])
    cp_sub = len(out["coupang"]["subordinate_listings"])
    ss_new = len(out["smartstore"]["new_options_no_listing"])
    cp_new = len(out["coupang"]["new_options_no_listing"])

    if not (out["smartstore"]["master"] or out["coupang"]["master"]):
        out["strategy"] = "no_action"
    elif ss_sub == 0 and cp_sub == 0:
        out["strategy"] = "options_add_only"      # archive 없음 — 가장 안전
    elif ss_sub <= 5 and cp_sub <= 5:
        out["strategy"] = "single_extend"          # 작은 통합
    else:
        out["strategy"] = "multi_extend"           # 큰 통합 (위험)

    out["impact"] = {
        "smartstore": {"archive": ss_sub, "new_options": ss_new},
        "coupang": {"archive": cp_sub, "new_options": cp_new},
    }
    return out


# ── 옵션 C-1: 네이버 extend_with_options ───────────────
def _naver_extend_with_options(origin_no: str, options_simple: list, options_combinations: list, base_price: int | None = None) -> Optional[dict]:
    """기존 originProduct 의 detailAttribute.optionInfo 에 options 셋팅 후 PUT.

    내부적으로 update_product (GET → merge → PUT + 금지태그 자동 strip) 활용.
    """
    from backend.purchase.services.naver_commerce_service import get_product, update_product
    current = get_product(str(origin_no))
    if not current:
        logger.warning(f"[naver-extend] {origin_no} 조회 실패")
        return None
    detail = (current.get("originProduct") or {}).get("detailAttribute") or {}
    detail["optionInfo"] = {
        "simpleOptionSortType": "CREATE",
        "optionSimple": options_simple,
        "optionCombinationSortType": "CREATE",
        "optionCombinations": options_combinations,
        "useStockManagement": True,
    }
    partial = {"originProduct": {"detailAttribute": detail}}
    if base_price is not None:
        partial["originProduct"]["salePrice"] = int(base_price)
    return update_product(str(origin_no), partial)


def _naver_suspend_listing(origin_no: str) -> Optional[dict]:
    """네이버 listing 판매중지 (statusType='SUSPENSION')."""
    from backend.purchase.services.naver_commerce_service import update_product
    return update_product(str(origin_no), {"originProduct": {"statusType": "SUSPENSION"}})


def _naver_delete_listing(origin_no: str) -> bool:
    """네이버 listing 완전 삭제. 실패 시 SUSPENSION fallback."""
    from backend.purchase.services.naver_commerce_service import delete_product as _ss_delete
    ok, msg = _ss_delete(str(origin_no))
    if ok:
        return True
    logger.warning(f"[naver-delete] {origin_no} DELETE 실패 ({msg}) → SUSPENSION fallback")
    r = _naver_suspend_listing(str(origin_no))
    return bool(r)


def _coupang_delete_listing(seller_product_id: str) -> bool:
    """쿠팡 listing 완전 삭제. 실패 시 stop_sales fallback."""
    from backend.purchase.services.coupang_service import delete_product as _cp_delete
    ok, msg = _cp_delete(str(seller_product_id))
    if ok:
        return True
    logger.warning(f"[coupang-delete] {seller_product_id} DELETE 실패 ({msg}) → stop_sales fallback")
    ok2, _ = _coupang_stop_sales(str(seller_product_id))
    return ok2


# ── 옵션 C-2: 쿠팡 extend_with_items ────────────────────
def _coupang_extend_with_items(seller_product_id: str, new_items: list) -> Optional[dict]:
    """기존 sellerProduct 의 items 배열에 신규 vendorItem 추가 PUT (재승인).

    GET seller-products/{id} → items 추가 (중복 SKU 방지) → PUT.
    """
    from backend.purchase.services.coupang_service import (
        get_seller_product, _signature, BASE, _request_with_retry,
    )
    body = get_seller_product(str(seller_product_id))
    if not body:
        logger.warning(f"[coupang-extend] {seller_product_id} 조회 실패")
        return None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        logger.warning(f"[coupang-extend] {seller_product_id} data 응답 형식 예외")
        return None
    existing_items = list(data.get("items") or [])
    existing_skus = {it.get("externalVendorSku") for it in existing_items if isinstance(it, dict)}
    appended = 0
    for new_it in new_items:
        if not isinstance(new_it, dict):
            continue
        if new_it.get("externalVendorSku") in existing_skus:
            continue
        existing_items.append(new_it)
        appended += 1
    data["items"] = existing_items
    if appended == 0:
        logger.info(f"[coupang-extend] {seller_product_id} 추가할 items 0건 (이미 등록됨)")
        return {"data": seller_product_id, "code": "SUCCESS", "_no_change": True}

    path = f"/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{seller_product_id}"
    try:
        r = _request_with_retry(
            "PUT", BASE + path,
            headers=_signature("PUT", path),
            json=data,
            timeout=30,
        )
    except Exception as e:
        logger.error(f"[coupang-extend] {seller_product_id} PUT 예외: {e}")
        return None
    if r is None or r.status_code >= 400:
        logger.error(f"[coupang-extend] {seller_product_id} PUT 실패: {r.status_code if r else 'no-resp'} {r.text[:300] if r else ''}")
        return None
    return r.json()


def _coupang_stop_sales(seller_product_id: str) -> tuple[bool, str]:
    from backend.purchase.services.coupang_service import stop_sales
    return stop_sales(str(seller_product_id))


# ── 옵션 B 모드: 신규 multi-option 등록 + listing_options 매핑 ────

def _extract_smartstore_option_ids(origin_product_no: str) -> dict[str, str]:
    """등록 후 GET originProduct → optionCombinations 의 sellerManagerCode (child ASIN) 별 매핑.

    네이버는 옵션별 별도 channelProductNo 가 없음 — sellerManagerCode 가 옵션 식별자.
    반환: {child_asin: option_combination_id_or_seller_code}
    """
    from backend.purchase.services.naver_commerce_service import get_product
    current = get_product(str(origin_product_no))
    if not current:
        return {}
    op = current.get("originProduct") or {}
    opt_info = op.get("detailAttribute", {}).get("optionInfo") or {}
    combos = opt_info.get("optionCombinations") or []
    out = {}
    for c in combos:
        if not isinstance(c, dict):
            continue
        smc = c.get("sellerManagerCode")
        if smc:
            # network identifier — 우리는 sellerManagerCode 자체 사용 (= child ASIN)
            out[smc] = smc
    return out


def _extract_coupang_option_ids(seller_product_id: str) -> dict[str, str]:
    """등록 후 GET seller-products → items[i].vendorItemId 추출.

    items[i].externalVendorSku = child ASIN 이라 매칭.
    반환: {child_asin: vendor_item_id}
    """
    from backend.purchase.services.coupang_service import get_seller_product
    body = get_seller_product(str(seller_product_id))
    if not body:
        return {}
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return {}
    items = data.get("items") or []
    out = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        sku = it.get("externalVendorSku")
        vid = it.get("vendorItemId")
        if sku and vid:
            out[str(sku)] = str(vid)
    return out


def _persist_group_listing(
    channel: str,
    master_child_id: int,
    channel_product_id: str,
    sale_krw: int,
    cost_krw: int,
    fee_rate: float,
    net_margin_krw: int,
    options: list[dict],
    option_id_map: dict[str, str],
    coupang_category_code: int | None = None,
    smartstore_channel_no: str | None = None,
) -> int:
    """listings_pa INSERT (master) + listing_options INSERT (모든 옵션).

    options: [{"child_asin", "child_product_id", "option_label", "sale_krw",
               "cost_krw", "net_margin_krw", "stock"}, ...]
    """
    from backend.purchase.database import get_db
    ts = _now_iso()
    with get_db() as conn:
        # listings_pa INSERT (master child 기준)
        cur = conn.execute(
            """INSERT INTO listings_pa
                (product_id, channel, status, sale_krw, cost_krw_snapshot,
                 fee_rate, net_margin_krw, channel_product_id,
                 has_options, last_synced_at, coupang_category_code,
                 smartstore_channel_no)
               VALUES (?, ?, 'listed', ?, ?, ?, ?, ?, 1, ?, ?, ?)
               ON CONFLICT(product_id, channel) DO UPDATE SET
                 channel_product_id=excluded.channel_product_id,
                 status='listed',
                 has_options=1,
                 sale_krw=excluded.sale_krw,
                 cost_krw_snapshot=excluded.cost_krw_snapshot,
                 fee_rate=excluded.fee_rate,
                 net_margin_krw=excluded.net_margin_krw,
                 last_synced_at=excluded.last_synced_at,
                 smartstore_channel_no=COALESCE(excluded.smartstore_channel_no,
                                                listings_pa.smartstore_channel_no)""",
            (master_child_id, channel, sale_krw, cost_krw, fee_rate,
             net_margin_krw, channel_product_id, ts, coupang_category_code,
             smartstore_channel_no),
        )
        listing_id = cur.lastrowid
        if not listing_id:
            row = conn.execute(
                "SELECT id FROM listings_pa WHERE product_id=? AND channel=?",
                (master_child_id, channel),
            ).fetchone()
            listing_id = row["id"] if row else None
        if not listing_id:
            raise RuntimeError("listings_pa INSERT 실패")

        # listing_options 다대일 INSERT
        for opt in options:
            asin = opt["child_asin"]
            channel_option_id = option_id_map.get(asin)
            conn.execute(
                """INSERT INTO listing_options
                    (listing_id, child_product_id, option_label, channel_option_id,
                     sale_krw, cost_krw_snapshot, net_margin_krw, stock,
                     status, last_synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                   ON CONFLICT(listing_id, child_product_id) DO UPDATE SET
                     option_label=excluded.option_label,
                     channel_option_id=excluded.channel_option_id,
                     sale_krw=excluded.sale_krw,
                     last_synced_at=excluded.last_synced_at""",
                (listing_id, opt["child_product_id"], opt["option_label"],
                 channel_option_id, opt.get("sale_krw"),
                 opt.get("cost_krw"), opt.get("net_margin_krw"),
                 opt.get("stock") or 100, ts),
            )
    return listing_id


# ── B09 큰 그룹 데이터 보강 — children INSERT + cost 책정 ────────

def _get_buybox_or_lowest_price(asin: str) -> Optional[float]:
    """SP-API ProductPricing get_item_offers → BuyBox(New) > LowestPrice(Amazon) > LowestPrice(Merchant)."""
    try:
        from sp_api.api import Products
        from sp_api.base import Marketplaces
        from backend.dropshipping.services.amazon_sp_api_service import get_credentials
    except ImportError:
        return None
    try:
        creds = get_credentials()
        api = Products(credentials=creds, marketplace=Marketplaces.US)
        res = api.get_item_offers(asin=asin, item_condition="New", customer_type="Consumer")
        payload = res.payload or {}
        summary = payload.get("Summary") or {}
        # 1. BuyBoxPrices (New)
        for bb in (summary.get("BuyBoxPrices") or []):
            cond = (bb.get("condition") or "").lower()
            if cond == "new":
                lp = bb.get("LandedPrice") or {}
                if lp.get("Amount"):
                    return float(lp["Amount"])
        # 2. LowestPrices — Amazon 직판 우선
        for it in (summary.get("LowestPrices") or []):
            if (it.get("fulfillmentChannel") or "").lower() == "amazon":
                lp = it.get("LandedPrice") or {}
                if lp.get("Amount"):
                    return float(lp["Amount"])
        # 3. LowestPrices — Merchant
        for it in (summary.get("LowestPrices") or []):
            lp = it.get("LandedPrice") or {}
            if lp.get("Amount"):
                return float(lp["Amount"])
    except Exception as e:
        logger.warning(f"[pricing] {asin}: {e}")
    return None


def fetch_and_insert_children(parent_asin: str, job_id: Optional[str] = None) -> dict:
    """variation_groups.child_asins 중 products 에 없는 ASIN → SP-API + Pricing API 동시 호출 → INSERT.

    B안 (per-ASIN 병렬): 한 ASIN당 CatalogItems / getItemOffers 두 호출을 ThreadPoolExecutor 로 동시 진행,
    INSERT 시점에 cost_usd 같이 채움 (BuyBox > Lowest > master fallback).
    Pricing API 1 RPS 제한이 bottleneck → ASIN 간엔 sleep(1.0) 유지 (sequential).
    """
    import time as _time
    from concurrent.futures import ThreadPoolExecutor
    from backend.purchase.database import get_db
    from backend.purchase.services.sp_api_facts import fetch_full_catalog_facts

    with get_db() as conn:
        vg = conn.execute(
            "SELECT child_asins_json FROM variation_groups WHERE parent_asin=?",
            (parent_asin,),
        ).fetchone()
    if not vg:
        return {"error": "variation_groups 없음"}
    try:
        child_asins = json.loads(vg["child_asins_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        child_asins = []
    if not child_asins:
        return {"error": "child_asins 비어있음"}

    with get_db() as conn:
        ph = ",".join("?" * len(child_asins))
        existing = conn.execute(
            f"SELECT asin FROM products WHERE asin IN ({ph})", child_asins,
        ).fetchall()
        master_row = conn.execute(
            "SELECT cost_usd FROM products WHERE asin=? AND cost_usd > 0 LIMIT 1",
            (parent_asin,),
        ).fetchone()
    existing_set = {r["asin"] for r in existing}
    target = [a for a in child_asins if a not in existing_set]
    total = len(target)
    master_cost = float(master_row["cost_usd"]) if master_row else 0.0

    inserted = errors = 0
    cost_buybox = cost_fallback = cost_no_data = 0
    for i, asin in enumerate(target, 1):
        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                f_facts = ex.submit(fetch_full_catalog_facts, asin, persist=False)
                f_price = ex.submit(_get_buybox_or_lowest_price, asin)
                facts = f_facts.result()
                price = f_price.result()
            if not facts:
                errors += 1
                _time.sleep(1.0)
                continue
            if price and price > 0:
                cost_usd = price
                cost_buybox += 1
            elif master_cost > 0:
                cost_usd = master_cost
                cost_fallback += 1
            else:
                cost_usd = None
                cost_no_data += 1
            with get_db() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO products
                        (asin, title_en, brand, business_model, status,
                         parent_asin, sp_api_facts_json, sp_api_facts_at,
                         weight_g, images_json, group_master_asin, cost_usd)
                       VALUES (?, ?, ?, 'purchase', 'draft', ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        asin,
                        facts.get("title_en", ""),
                        facts.get("brand") or facts.get("manufacturer"),
                        parent_asin,
                        json.dumps(facts, ensure_ascii=False),
                        facts.get("fetched_at"),
                        facts.get("item_weight_g") or facts.get("item_display_weight_g"),
                        json.dumps(facts.get("images") or [], ensure_ascii=False),
                        parent_asin,
                        cost_usd,
                    ),
                )
            inserted += 1
        except Exception as e:
            errors += 1
            logger.warning(f"[insert-children] {asin}: {e}")

        if job_id and i % 30 == 0:
            with get_db() as conn:
                conn.execute(
                    """UPDATE batch_jobs SET processed=?, phase_message=? WHERE id=?""",
                    (
                        i,
                        f"Stage1 facts+cost {i}/{total} (ok {inserted} / err {errors} / BB {cost_buybox} / fb {cost_fallback})",
                        job_id,
                    ),
                )
        _time.sleep(1.0)   # Pricing API 1 RPS 제한
    return {
        "target": total,
        "inserted": inserted,
        "errors": errors,
        "skipped_existing": len(existing_set),
        "cost_buybox": cost_buybox,
        "cost_fallback": cost_fallback,
        "cost_no_data": cost_no_data,
    }


def assign_cost_via_pricing(parent_asin: str, job_id: Optional[str] = None,
                             fallback_master_cost: bool = True,
                             processed_offset: int = 0) -> dict:
    """variation_groups 의 children 중 cost_usd NULL 인 product 에 BuyBox/Lowest 가격 책정.

    BuyBox(New) → LowestPrice(Amazon) → LowestPrice(Merchant) → master fallback.
    Stage 2 진입 시 processed_offset 으로 Stage 1 누적량 보존 (progress bar 리셋 방지).
    """
    import time as _time
    from backend.purchase.database import get_db

    with get_db() as conn:
        vg = conn.execute(
            "SELECT child_asins_json FROM variation_groups WHERE parent_asin=?",
            (parent_asin,),
        ).fetchone()
    if not vg:
        return {"error": "variation_groups 없음"}
    try:
        child_asins = json.loads(vg["child_asins_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        child_asins = []
    if not child_asins:
        return {"error": "child_asins 비어있음"}

    with get_db() as conn:
        ph = ",".join("?" * len(child_asins))
        rows = conn.execute(
            f"SELECT id, asin, cost_usd FROM products WHERE asin IN ({ph})",
            child_asins,
        ).fetchall()

    master_cost = None
    for r in rows:
        if r["cost_usd"] and float(r["cost_usd"]) > 0:
            master_cost = float(r["cost_usd"])
            break

    target = [r for r in rows if not (r["cost_usd"] and float(r["cost_usd"]) > 0)]
    total = len(target)
    if total == 0:
        return {"target": 0, "ok": 0, "fallback_master": 0, "no_data": 0}

    ok = fallback_used = no_data = 0
    for i, r in enumerate(target, 1):
        price = _get_buybox_or_lowest_price(r["asin"])
        if price is not None and price > 0:
            ok += 1
        elif fallback_master_cost and master_cost:
            price = master_cost
            fallback_used += 1
        else:
            no_data += 1
            _time.sleep(1.0)
            continue
        with get_db() as conn:
            conn.execute("UPDATE products SET cost_usd=? WHERE id=?", (price, r["id"]))

        if job_id and i % 30 == 0:
            done = ok + fallback_used + no_data
            with get_db() as conn:
                conn.execute(
                    """UPDATE batch_jobs SET processed=?, phase_message=? WHERE id=?""",
                    (
                        processed_offset + done,
                        f"Stage2 cost 책정 {done}/{total} (BuyBox {ok} / fallback {fallback_used} / no_data {no_data})",
                        job_id,
                    ),
                )
        _time.sleep(1.0)   # rate limit 0.5 RPS = 1초/req

    return {"target": total, "ok": ok, "fallback_master": fallback_used, "no_data": no_data}


def run_backfill_job(job_id: str, parent_asin: str) -> None:
    """백그라운드 잡: per-ASIN 병렬 facts+cost INSERT (Stage 1) + 잔여 row cost 책정 (Stage 2)."""
    from backend.purchase.database import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE batch_jobs SET status='running', started_at=datetime('now'), phase_message='Stage1 시작 (병렬 facts+cost)' WHERE id=?",
            (job_id,),
        )
    try:
        r1 = fetch_and_insert_children(parent_asin, job_id=job_id)
        r2 = assign_cost_via_pricing(
            parent_asin, job_id=job_id,
            processed_offset=r1.get("target", 0),
        )
        bb_total = r1.get("cost_buybox", 0) + r2.get("ok", 0)
        fb_total = r1.get("cost_fallback", 0) + r2.get("fallback_master", 0)
        nd_total = r1.get("cost_no_data", 0) + r2.get("no_data", 0)
        msg = (
            f"완료: Stage1 {r1.get('inserted', 0)}/{r1.get('target', 0)} "
            f"(skip {r1.get('skipped_existing', 0)}) | "
            f"Stage2 {r2.get('ok', 0) + r2.get('fallback_master', 0)}/{r2.get('target', 0)} | "
            f"cost: BuyBox {bb_total} / fallback {fb_total} / no_data {nd_total}"
        )
        with get_db() as conn:
            conn.execute(
                """UPDATE batch_jobs SET status='done', finished_at=datetime('now'), phase_message=? WHERE id=?""",
                (msg, job_id),
            )
    except Exception as e:
        logger.exception(f"[backfill-job] {job_id} {parent_asin} 실패")
        with get_db() as conn:
            conn.execute(
                """UPDATE batch_jobs SET status='error', finished_at=datetime('now'), error_message=? WHERE id=?""",
                (str(e)[:500], job_id),
            )


def ai_fill_mandatory(facts: dict, category_code: str) -> Optional[dict]:
    """Gemini 로 카테고리 mandatory attribute 자동 채움. {attributeTypeName: {"value":..,"unit":..}} 반환."""
    if not facts or not category_code or category_code == "0":
        return None
    try:
        from backend.purchase.services.coupang_meta import get_required_attributes
        from backend_shared.ai.service import _call_ai_sync
    except ImportError:
        return None

    required = get_required_attributes(str(category_code))
    if not required:
        return {}

    schema_lines = []
    for a in required:
        name = a.get("attributeTypeName")
        dt = a.get("dataType")
        units = [u.get("unitName") for u in (a.get("basicUnits") or []) if u.get("unitName")]
        schema_lines.append(f"  - {name} (dataType={dt}, units={units})")

    facts_summary = {
        "title_en": facts.get("title_en"),
        "brand": facts.get("brand"),
        "bullet_points": (facts.get("bullet_points") or [])[:3],
        "item_dimensions": facts.get("item_dimensions"),
        "item_weight_g": facts.get("item_weight_g"),
        "color": facts.get("color"),
        "size_label": facts.get("size_label"),
        "material": facts.get("material"),
        "package_quantity": facts.get("package_quantity"),
        "browse_classification": facts.get("browse_classification"),
    }

    prompt = (
        f"Amazon 상품을 한국 쿠팡 카테고리({category_code}) mandatory attribute 에 매핑.\n\n"
        f"facts:\n{json.dumps(facts_summary, ensure_ascii=False, indent=2)}\n\n"
        f"mandatory attributes:\n" + "\n".join(schema_lines) + "\n\n"
        f"규칙:\n"
        f"- 수량 항상 1 (단위 '개' or '세트')\n"
        f"- 사이즈/용량은 bullet_points/title 에서 가방·제품 자체 용량 추출 (예: '18L'). 없으면 '원사이즈'\n"
        f"- 무게는 item_weight_g (>=1000g 이면 kg)\n"
        f"- 색상/재질 한글 변환\n"
        f"- facts 에 없으면 일반 default ('기타','없음', null)\n\n"
        f"응답: JSON 만 (설명 X)\n"
        f"{{\"attribute name\": {{\"value\": ..., \"unit\": ...}}, ...}}\n"
    )

    res = _call_ai_sync(prompt, max_tokens=2000)
    if not res:
        return None
    res = res.strip()
    if res.startswith("```"):
        res = "\n".join(l for l in res.split("\n") if not l.startswith("```"))
    try:
        return json.loads(res)
    except Exception as e:
        logger.warning(f"[ai-mandatory] JSON 파싱 실패: {e} | response: {res[:300]}")
        return None


def _get_or_compute_mandatory_attrs(parent_asin: str, master_facts: dict, category_code: str) -> dict:
    """variation_groups.mandatory_attrs_json 캐시 우선. miss 시 AI 호출 후 저장."""
    if not parent_asin:
        return {}
    from backend.purchase.database import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT mandatory_attrs_json FROM variation_groups WHERE parent_asin=?",
            (parent_asin,),
        ).fetchone()
    if row and row["mandatory_attrs_json"]:
        try:
            return json.loads(row["mandatory_attrs_json"])
        except Exception:
            pass
    res = ai_fill_mandatory(master_facts, category_code)
    if res is None:
        return {}
    with get_db() as conn:
        conn.execute(
            "UPDATE variation_groups SET mandatory_attrs_json=? WHERE parent_asin=?",
            (json.dumps(res, ensure_ascii=False), parent_asin),
        )
    return res


def _disk_usage_pct(path: str = "/") -> float:
    """디스크 사용률 % (셸 호출 없이)."""
    import shutil
    total, used, _ = shutil.disk_usage(path)
    return (used / total) * 100 if total else 0.0


def register_groups_batch(
    parent_asins: list[str],
    job_id: str,
    channels: list[str] | None = None,
    sleep_between: float = 5.0,
    max_consecutive_failures: int = 5,
    disk_threshold_pct: float = 85.0,
) -> dict:
    """순차로 register_new_group_listing 호출 + batch_jobs progress + 안전장치.

    중단 조건:
      - 디스크 > disk_threshold_pct
      - 연속 실패 ≥ max_consecutive_failures
      - batch_jobs.status='cancelled' (사용자 중단)
    """
    import time as _time
    from backend.purchase.database import get_db

    channels = channels or ["smartstore", "coupang"]
    total = len(parent_asins)
    success = skipped = failed = 0
    consecutive_fail = 0
    aborted_reason = None

    with get_db() as conn:
        conn.execute(
            "UPDATE batch_jobs SET status='running', started_at=datetime('now'), phase_message='batch register 시작' WHERE id=?",
            (job_id,),
        )

    for i, p in enumerate(parent_asins, 1):
        # 사용자 중단 / 디스크 체크
        with get_db() as conn:
            row = conn.execute("SELECT status FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
        if row and row["status"] == "cancelled":
            aborted_reason = "사용자 중단"; break
        disk_pct = _disk_usage_pct("/")
        if disk_pct > disk_threshold_pct:
            aborted_reason = f"디스크 {disk_pct:.1f}% > {disk_threshold_pct}%"; break

        try:
            res = register_new_group_listing(p, channels=channels, dry_run=False)
            ch_results = res.get("channels") or {}
            grp_success = grp_fail = 0
            for ch, items in ch_results.items():
                if not isinstance(items, list):
                    continue
                for it in items:
                    s = it.get("status")
                    if s == "registered": grp_success += 1
                    elif s and s != "_summary" and s != "dry_run": grp_fail += 1
            if grp_success > 0 and grp_fail == 0:
                success += 1; consecutive_fail = 0
            elif grp_success > 0:
                success += 1; consecutive_fail = 0
            else:
                failed += 1; consecutive_fail += 1
        except Exception as e:
            logger.exception(f"[batch-register] {p} 예외")
            failed += 1; consecutive_fail += 1

        with get_db() as conn:
            conn.execute(
                """UPDATE batch_jobs SET processed=?, phase_message=? WHERE id=?""",
                (i, f"진행 {i}/{total} (성공 {success} / 실패 {failed} / 디스크 {disk_pct:.1f}%)", job_id),
            )

        if consecutive_fail >= max_consecutive_failures:
            aborted_reason = f"연속 실패 {consecutive_fail}회"; break
        _time.sleep(sleep_between)

    msg = (
        f"완료: 성공 {success} / 실패 {failed} / 처리 {i if 'i' in dir() else 0}/{total}"
        + (f" | 중단: {aborted_reason}" if aborted_reason else "")
    )
    final_status = "error" if aborted_reason else "done"
    with get_db() as conn:
        conn.execute(
            """UPDATE batch_jobs SET status=?, finished_at=datetime('now'), phase_message=? WHERE id=?""",
            (final_status, msg, job_id),
        )
    return {"success": success, "failed": failed, "total": total, "aborted": aborted_reason}


def ensure_promoted(product_id: int) -> bool:
    """promote 후처리: sale_price_krw, category_path, detail_pages 채움. 모두 OK 시 True."""
    from backend.purchase.database import get_db

    with get_db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not p:
            return False
        product = dict(p)

    # 1. sale_price_krw
    if not product.get("sale_price_krw") or float(product.get("sale_price_krw") or 0) <= 0:
        try:
            from backend.purchase.services.pricing_service_pa import calculate_sale_krw
            r = calculate_sale_krw(
                cost_usd=float(product.get("cost_usd") or 0),
                channel="smartstore",
            )
            sale_krw = (r.get("sale_krw") if isinstance(r, dict) else None) or 0
            if sale_krw > 0:
                with get_db() as conn:
                    conn.execute("UPDATE products SET sale_price_krw=? WHERE id=?", (int(sale_krw), product_id))
                product["sale_price_krw"] = int(sale_krw)
        except Exception as e:
            logger.warning(f"[ensure-promoted] {product_id} pricing fail: {e}")

    # 2. category_path — title_ko 보강 후 score 기반 매핑
    if not product.get("category_path"):
        try:
            # title_ko 자동 번역 (영문 title 일 때 카테고리 매핑 정확도 ↑)
            if product.get("asin"):
                from backend.purchase.services.title_translator import ensure_title_ko
                ko = ensure_title_ko(product["asin"])
                if ko:
                    product["title_ko"] = ko

            from backend_shared.category_service import find_category_with_gemini
            r = find_category_with_gemini(
                product_name=product.get("title_ko") or product.get("title_en") or "",
            ) or {}
            cat_id = str(r.get("id") or "").strip()
            score = int(r.get("score") or 0)
            needs_review = bool(r.get("needs_review", True))

            if cat_id and not needs_review:
                with get_db() as conn:
                    conn.execute("UPDATE products SET category_path=? WHERE id=?", (cat_id, product_id))
                product["category_path"] = cat_id
                logger.info(f"[ensure-promoted] {product_id} category={cat_id} score={score} ({r.get('whole_name','')})")
            elif cat_id:
                # score < 50 → 자동 적용 안 함, review 큐 (Fix 1-D 에서 처리)
                logger.warning(f"[ensure-promoted] {product_id} category={cat_id} score={score} <50 — review 필요 ({r.get('whole_name','')})")
            else:
                logger.warning(f"[ensure-promoted] {product_id} category 매핑 실패")
        except Exception as e:
            logger.warning(f"[ensure-promoted] {product_id} category fail: {e}")

    # 3. detail_pages
    with get_db() as conn:
        has_detail = conn.execute(
            "SELECT 1 FROM detail_pages WHERE product_id=? AND html_content IS NOT NULL AND html_content != '' LIMIT 1",
            (product_id,),
        ).fetchone()
    if not has_detail:
        try:
            from backend_shared.detail_page_service import generate_detail_page
            generate_detail_page(product=product, market="KR", platform="smartstore")
        except Exception as e:
            logger.warning(f"[ensure-promoted] {product_id} detail fail: {e}")

    # 검증
    with get_db() as conn:
        p2 = conn.execute("SELECT sale_price_krw, category_path FROM products WHERE id=?", (product_id,)).fetchone()
        d = conn.execute(
            "SELECT 1 FROM detail_pages WHERE product_id=? AND html_content IS NOT NULL AND html_content != '' LIMIT 1",
            (product_id,),
        ).fetchone()
    return bool(p2 and (p2["sale_price_krw"] or 0) > 0 and p2["category_path"] and d)


def pick_master_asin(parent_asin: str) -> Optional[str]:
    """그룹의 master child ASIN 선정. master_asin 우선, 없으면 첫 product."""
    from backend.purchase.database import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT master_asin FROM variation_groups WHERE parent_asin=?", (parent_asin,),
        ).fetchone()
        if row and row["master_asin"]:
            return row["master_asin"]
        row2 = conn.execute(
            """SELECT asin FROM products
               WHERE parent_asin=? AND status IN ('draft','active','listed')
               ORDER BY id LIMIT 1""",
            (parent_asin,),
        ).fetchone()
        return row2["asin"] if row2 else None


def register_master_singletons_batch(
    parent_asins: list[str],
    job_id: str,
    channels: list[str] | None = None,
    sleep_between: float = 5.0,
    max_consecutive_failures: int = 5,
    disk_threshold_pct: float = 85.0,
) -> dict:
    """그룹별 master child 1건만 단일 listing 등록 (검증된 흐름).

    각 그룹의 master_asin → product → smartstore_lister/coupang_lister.list_product 호출.
    카테고리 자동매칭, image_cache 다운로드, 검수 등은 lister 가 처리.
    """
    import time as _time
    from backend.purchase.database import get_db
    from backend.purchase.services.smartstore_lister import list_product as ss_list
    from backend.purchase.services.coupang_lister import list_product as cp_list

    channels = channels or ["smartstore", "coupang"]
    total = len(parent_asins)
    success = failed = 0
    consecutive_fail = 0
    aborted = None
    i = 0

    with get_db() as conn:
        conn.execute(
            "UPDATE batch_jobs SET status='running', started_at=datetime('now'), phase_message='master 단일 등록 시작' WHERE id=?",
            (job_id,),
        )

    for i, parent in enumerate(parent_asins, 1):
        with get_db() as conn:
            row = conn.execute("SELECT status FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
        if row and row["status"] == "cancelled":
            aborted = "사용자 중단"; break
        disk_pct = _disk_usage_pct("/")
        if disk_pct > disk_threshold_pct:
            aborted = f"디스크 {disk_pct:.1f}%"; break

        master_asin = pick_master_asin(parent)
        if not master_asin:
            failed += 1; consecutive_fail += 1; continue
        with get_db() as conn:
            prow = conn.execute("SELECT id FROM products WHERE asin=? LIMIT 1", (master_asin,)).fetchone()
        if not prow:
            failed += 1; consecutive_fail += 1; continue
        product_id = prow["id"]

        # promote 후처리 (가격, 카테고리, detail)
        ensure_promoted(product_id)

        any_ok = False
        for ch in channels:
            try:
                if ch == "smartstore":
                    res = ss_list(product_id)
                else:
                    res = cp_list(product_id)
                if res and res.get("ok"):
                    any_ok = True
            except Exception:
                logger.exception(f"[master-singleton] {parent}/{master_asin} {ch} 예외")

        if any_ok:
            success += 1; consecutive_fail = 0
        else:
            failed += 1; consecutive_fail += 1

        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET processed=?, phase_message=? WHERE id=?",
                (i, f"진행 {i}/{total} (성공 {success} / 실패 {failed} / 디스크 {disk_pct:.1f}%)", job_id),
            )
        if consecutive_fail >= max_consecutive_failures:
            aborted = f"연속 실패 {consecutive_fail}회"; break
        _time.sleep(sleep_between)

    final_status = "error" if aborted else "done"
    msg = f"완료: 성공 {success} / 실패 {failed} / 처리 {i}/{total}" + (f" | 중단: {aborted}" if aborted else "")
    with get_db() as conn:
        conn.execute(
            "UPDATE batch_jobs SET status=?, finished_at=datetime('now'), phase_message=? WHERE id=?",
            (final_status, msg, job_id),
        )
    return {"success": success, "failed": failed, "total": total, "aborted": aborted}


def backfill_listing_options_channel_ids(listing_id: int) -> dict:
    """listing_options 의 channel_option_id NULL 인 row 들을 채널 GET 으로 backfill.

    register 단계에서 매핑 실패 시 별도 호출 가능 (UI 버튼 또는 자동 잡).
    """
    from backend.purchase.database import get_db
    with get_db() as conn:
        listing = conn.execute(
            "SELECT id, channel, channel_product_id FROM listings_pa WHERE id=?",
            (listing_id,),
        ).fetchone()
        if not listing or not listing["channel_product_id"]:
            return {"error": "listing 없음 또는 channel_product_id 미채움"}
        opts = conn.execute(
            """SELECT lo.id AS lo_id, p.asin
               FROM listing_options lo JOIN products p ON p.id = lo.child_product_id
               WHERE lo.listing_id=? AND (lo.channel_option_id IS NULL OR lo.channel_option_id='')""",
            (listing_id,),
        ).fetchall()
    if not opts:
        return {"filled": 0, "total": 0, "message": "이미 모두 매핑됨"}

    if listing["channel"] == "smartstore":
        id_map = _extract_smartstore_option_ids(listing["channel_product_id"])
    else:
        id_map = _extract_coupang_option_ids(listing["channel_product_id"])

    filled = 0
    with get_db() as conn:
        for o in opts:
            cid = id_map.get(o["asin"])
            if cid:
                conn.execute(
                    "UPDATE listing_options SET channel_option_id=?, last_synced_at=datetime('now') WHERE id=?",
                    (cid, o["lo_id"]),
                )
                filled += 1
    return {"filled": filled, "total": len(opts)}


def register_new_group_listing(
    parent_asin: str,
    channels: list[str] | None = None,
    dry_run: bool = True,
    split_indices: list[int] | None = None,
) -> dict:
    """B 모드: 옵션 그룹을 처음부터 multi-option 으로 신규 등록.

    extend (A 모드) 와 차이:
      - 기존 master listing 무시 — 그룹의 children 으로 master 결정
      - register_product (POST) 로 신규 등록
      - 응답에서 channel_product_id 받음 → GET 으로 옵션별 ID 추출
      - listings_pa INSERT (master_child_id 기준) + listing_options 다대일 INSERT
      - 같은 group 의 단일 listing 들은 사후 archive (옵션 C 와 동일)
    """
    from backend.purchase.services.variation import (
        load_group, auto_split, calculate_group_pricing,
    )
    from backend.purchase.database import get_db

    channels = channels or ["smartstore", "coupang"]
    group = load_group(parent_asin)
    if not group:
        return {"error": f"group {parent_asin} 없음"}

    out = {"parent_asin": parent_asin, "mode": "register", "dry_run": dry_run, "channels": {}}
    analysis = analyze_group_listings(parent_asin)
    out["analysis"] = analysis

    for ch in channels:
        splits = auto_split(group, ch)
        pricing = calculate_group_pricing(group, ch)
        by_asin = {p["child_asin"]: p for p in pricing}

        ch_results = []
        for sp_idx, split in enumerate(splits):
            if split_indices is not None and sp_idx not in split_indices:
                continue
            opt_asins = [o.get("asin") for o in split.get("options") or []]
            sp_pricing = [by_asin[a] for a in opt_asins if a in by_asin]
            if not sp_pricing:
                ch_results.append({"split_index": sp_idx, "status": "skipped", "reason": "no pricing"})
                continue

            if ch == "smartstore":
                payload = build_smartstore_payload(group, split, sp_pricing)
            else:
                payload = build_coupang_payload(group, split, sp_pricing)
            if not payload:
                ch_results.append({"split_index": sp_idx, "status": "skipped", "reason": "payload build failed"})
                continue

            # master = split.options 첫 child 중 우리 products 에 있는 것 (= calculate_group_pricing 첫 항목)
            master_pricing = sp_pricing[0]
            master_child_id = master_pricing["child_product_id"]
            master_asin = master_pricing["child_asin"]

            if dry_run:
                ch_results.append({
                    "split_index": sp_idx,
                    "status": "dry_run",
                    "split_name": split.get("name"),
                    "master_child_asin": master_asin,
                    "master_child_id": master_child_id,
                    "options_count": len(sp_pricing),
                    "payload_keys": list(payload.keys()),
                })
                continue

            # ── 실등록 ──
            try:
                if ch == "smartstore":
                    from backend.purchase.services.naver_commerce_service import register_product as ss_register
                    res = ss_register(payload)
                    if not res or res.get("_error"):
                        ch_results.append({"split_index": sp_idx, "status": "register_failed",
                                           "error": (res or {}).get("_error", "no response")[:300]})
                        continue
                    origin_no = str(res.get("originProductNo") or "")
                    if not origin_no:
                        ch_results.append({"split_index": sp_idx, "status": "register_failed",
                                           "error": "originProductNo missing"})
                        continue
                    option_id_map = _extract_smartstore_option_ids(origin_no)
                    channel_product_id = origin_no
                    smartstore_channel_no = str(res.get("smartstoreChannelProductNo") or "") or None
                else:
                    from backend.purchase.services.coupang_service import register_product as cp_register, request_approval
                    res = cp_register(payload)
                    if not res or not res.get("data"):
                        ch_results.append({"split_index": sp_idx, "status": "register_failed",
                                           "error": str(res)[:300] if res else "no response"})
                        continue
                    seller_product_id = str(res["data"])
                    # 승인 요청은 페이로드 requested=True 로 자동 트리거됨 (별도 PUT 불필요)
                    option_id_map = _extract_coupang_option_ids(seller_product_id)
                    channel_product_id = seller_product_id
                    smartstore_channel_no = None

                # listings_pa + listing_options 매핑 INSERT
                base_pricing = master_pricing
                options_for_persist = []
                for p in sp_pricing:
                    options_for_persist.append({
                        "child_asin": p["child_asin"],
                        "child_product_id": p["child_product_id"],
                        "option_label": p.get("option_label") or "기본",
                        "sale_krw": p.get("sale_krw"),
                        "cost_krw": p.get("cost_krw"),
                        "net_margin_krw": p.get("net_margin_krw"),
                        "stock": 100,
                    })
                listing_id = _persist_group_listing(
                    channel=ch,
                    master_child_id=master_child_id,
                    channel_product_id=channel_product_id,
                    sale_krw=base_pricing.get("sale_krw"),
                    cost_krw=base_pricing.get("cost_krw"),
                    fee_rate=base_pricing.get("fee_rate", 0),
                    net_margin_krw=base_pricing.get("net_margin_krw"),
                    options=options_for_persist,
                    option_id_map=option_id_map,
                    coupang_category_code=(int(channel_product_id) if False else None),  # set below for coupang
                    smartstore_channel_no=smartstore_channel_no,
                )

                ch_results.append({
                    "split_index": sp_idx,
                    "status": "registered",
                    "channel_product_id": channel_product_id,
                    "listing_id": listing_id,
                    "options_persisted": len(options_for_persist),
                    "options_with_channel_id": sum(1 for o in options_for_persist
                                                    if option_id_map.get(o["child_asin"])),
                })
            except Exception as e:
                logger.exception(f"[register-new-group] {ch} split#{sp_idx} 실패")
                ch_results.append({"split_index": sp_idx, "status": "exception", "error": str(e)[:300]})

        # split loop 끝난 후 — 같은 group 의 기존 단일 listing 들 archive (channel 당 1회)
        # 이번 flow 가 만든 신규 listing IDs 는 절대 archive 대상에서 제외
        registered_ids = {r.get("listing_id") for r in ch_results if r.get("status") == "registered"}
        ch_info = analysis.get(ch, {})
        sub_listings = ch_info.get("subordinate_listings") or []
        old_master = ch_info.get("master")
        archive_targets = [sl for sl in sub_listings if sl.get("listing_id") not in registered_ids]
        if old_master and old_master.get("listing_id") not in registered_ids:
            archive_targets.append(old_master)

        archive_ok = 0
        for sl in archive_targets:
            cpi = sl.get("channel_product_id")
            if not cpi:
                continue
            # 채널에서 완전 삭제 (실패 시 helper 안에서 SUSPENSION/stop_sales fallback)
            if ch == "smartstore":
                ok = _naver_delete_listing(cpi)
            else:
                ok = _coupang_delete_listing(cpi)
            if ok:
                archive_ok += 1
                with get_db() as conn:
                    conn.execute(
                        "UPDATE listings_pa SET status='archived', error_message='[B모드 신규등록] master 신규 listing 채널 삭제' WHERE id=?",
                        (sl["listing_id"],),
                    )
        ch_results.append({
            "_summary": "archive_subordinates",
            "archived_subordinates": archive_ok,
            "archive_total": len(archive_targets),
        })

        out["channels"][ch] = ch_results

    return out


# ── 옵션 C-4: 통합 등록 진입점 (extend_master_with_group) ───
def extend_master_with_group(parent_asin: str, channels: list[str] | None = None,
                              dry_run: bool = True, mode: str = "auto",
                              split_indices: list[int] | None = None) -> dict:
    """옵션 통합 등록 진입점 — A 모드(extend) / B 모드(register) 통합.

    mode:
      'auto'    — analysis 의 master 유무로 자동 분기 (master 있으면 extend, 없으면 register)
      'extend'  — A 모드 강제 (기존 master listing 에 옵션만 추가)
      'register' — B 모드 강제 (신규 multi-option 등록 + 기존 단일 archive)

    dry_run=True 면 채널 호출 없이 페이로드만 빌드.
    """
    if mode == "auto":
        analysis_pre = analyze_group_listings(parent_asin)
        if "error" in analysis_pre:
            return {"error": analysis_pre["error"]}
        # master 가 어느 채널이든 있으면 extend, 모두 없으면 register
        has_master = any((analysis_pre.get(ch) or {}).get("master") for ch in (channels or ["smartstore", "coupang"]))
        mode = "extend" if has_master else "register"

    if mode == "register":
        result = register_new_group_listing(parent_asin, channels, dry_run=dry_run,
                                             split_indices=split_indices)
        result["mode"] = "register"
        return result

    # mode == 'extend' (기존 코드)
    from backend.purchase.services.variation import (
        load_group, auto_split, calculate_group_pricing,
    )
    from backend.purchase.database import get_db

    channels = channels or ["smartstore", "coupang"]
    analysis = analyze_group_listings(parent_asin)
    if "error" in analysis:
        return {"error": analysis["error"]}

    group = load_group(parent_asin)
    out = {"parent_asin": parent_asin, "mode": "extend", "dry_run": dry_run, "analysis": analysis, "channels": {}}

    for ch in channels:
        ch_info = analysis.get(ch) or {}
        master = ch_info.get("master")
        if not master:
            out["channels"][ch] = {"action": "skip", "reason": "master 없음 (listed listing 없음)"}
            continue

        sub_listings = ch_info.get("subordinate_listings") or []
        new_options = ch_info.get("new_options_no_listing") or []

        # master 가 속한 split 찾기
        splits = auto_split(group, ch)
        master_split = None
        for sp in splits:
            opt_asins = [o.get("asin") for o in sp.get("options") or []]
            if master["child_asin"] in opt_asins:
                master_split = sp
                break
        if not master_split:
            out["channels"][ch] = {"action": "error", "reason": "master 의 split 없음"}
            continue

        pricing = calculate_group_pricing(group, ch)
        by_asin = {p["child_asin"]: p for p in pricing}
        opt_asins = [o.get("asin") for o in master_split.get("options") or []]
        sp_pricing = [by_asin[a] for a in opt_asins if a in by_asin]

        if ch == "smartstore":
            payload = build_smartstore_payload(group, master_split, sp_pricing)
        else:
            payload = build_coupang_payload(group, master_split, sp_pricing)

        if not payload:
            out["channels"][ch] = {"action": "error", "reason": "페이로드 빌드 실패 (이미지/master 미보유 가능)"}
            continue

        if dry_run:
            out["channels"][ch] = {
                "action": "dry_run",
                "master_listing_id": master["listing_id"],
                "master_channel_product_id": master["channel_product_id"],
                "subordinate_count": len(sub_listings),
                "new_options_count": len(new_options),
                "options_in_payload": (
                    len(payload.get("originProduct", {}).get("detailAttribute", {}).get("optionInfo", {}).get("optionCombinations") or [])
                    if ch == "smartstore"
                    else len(payload.get("items") or [])
                ),
            }
            continue

        # ── 실등록 ──
        if ch == "smartstore":
            origin_no = master["channel_product_id"]
            oi = payload.get("originProduct", {}).get("detailAttribute", {}).get("optionInfo", {})
            base_price = payload.get("originProduct", {}).get("salePrice")
            res = _naver_extend_with_options(
                origin_no,
                oi.get("optionSimple") or [],
                oi.get("optionCombinations") or [],
                base_price=base_price,
            )
            if not res:
                out["channels"][ch] = {"action": "extend_failed", "stage": "naver_extend"}
                continue
            sub_results = []
            for sl in sub_listings:
                r2 = _naver_suspend_listing(sl["channel_product_id"])
                sub_results.append({"listing_id": sl["listing_id"], "ok": bool(r2)})
            with get_db() as conn:
                conn.execute("UPDATE listings_pa SET has_options=1 WHERE id=?", (master["listing_id"],))
                for sl in sub_listings:
                    conn.execute(
                        "UPDATE listings_pa SET status='archived', error_message='[옵션C 통합] master 에 옵션 추가, 채널 listing SUSPEND' WHERE id=?",
                        (sl["listing_id"],),
                    )
            out["channels"][ch] = {
                "action": "extended",
                "extend_ok": True,
                "subordinates_suspended": sum(1 for s in sub_results if s["ok"]),
                "subordinates_total": len(sub_listings),
            }
            continue

        # coupang
        seller_id = master["channel_product_id"]
        items = payload.get("items") or []
        master_sku = master["child_asin"]
        new_items = [it for it in items if it.get("externalVendorSku") != master_sku]
        res = _coupang_extend_with_items(seller_id, new_items)
        if not res:
            out["channels"][ch] = {"action": "extend_failed", "stage": "coupang_extend"}
            continue
        sub_results = []
        for sl in sub_listings:
            ok, msg = _coupang_stop_sales(sl["channel_product_id"])
            sub_results.append({"listing_id": sl["listing_id"], "ok": ok, "msg": msg[:100]})
        with get_db() as conn:
            conn.execute("UPDATE listings_pa SET has_options=1 WHERE id=?", (master["listing_id"],))
            for sl in sub_listings:
                conn.execute(
                    "UPDATE listings_pa SET status='archived', error_message='[옵션C 통합] master 에 items 추가, 채널 listing stop_sales' WHERE id=?",
                    (sl["listing_id"],),
                )
        out["channels"][ch] = {
            "action": "extended",
            "extend_ok": True,
            "needs_reapproval": True,
            "subordinates_stopped": sum(1 for s in sub_results if s["ok"]),
            "subordinates_total": len(sub_listings),
        }

    return out


# ── 옵션 차원 추출 헬퍼 ───────────────────────────────
def _extract_option_values(split: dict) -> list[dict]:
    """split.options 의 children 으로부터 차원별 unique 값 목록 추출.

    반환: [{"groupName": "사이즈", "values": ["14온스","20온스",...]}, ...]
    """
    from backend.purchase.services.variation import korean_label

    options = split.get("options") or []
    dim_keys = []
    for c in options:
        for k in ("size_label", "color", "flavor_attr", "style"):
            if c.get(k) and k not in dim_keys:
                dim_keys.append(k)
    DIM_GROUP_NAME = {
        "size_label": "사이즈", "color": "색상",
        "flavor_attr": "맛", "style": "스타일",
    }
    out = []
    for k in dim_keys:
        seen = []
        for c in options:
            v = c.get(k)
            if v and v not in seen:
                seen.append(v)
        if not seen:
            continue
        kor_values = [korean_label(v) or v for v in seen]
        out.append({"key": k, "groupName": DIM_GROUP_NAME[k], "values": kor_values, "raw_values": seen})
    return out


def _option_label_for_child(child: dict, dim_groups: list[dict]) -> str:
    from backend.purchase.services.variation import korean_label
    parts = []
    for dg in dim_groups:
        v = child.get(dg["key"])
        if v:
            parts.append(korean_label(v) or v)
    return " / ".join(parts) if parts else "기본"


# ── master child 메타 (이미지·detailContent·category) ──
def _load_master_meta(master_asin: str) -> dict:
    """master child 의 products / detail_pages / images 모음."""
    from backend.purchase.database import get_db

    out = {"product": None, "detail_html": "", "image_urls": [], "category_path": "",
           "coupang_category_code": None}
    if not master_asin:
        return out
    with get_db() as conn:
        p = conn.execute(
            "SELECT * FROM products WHERE asin=? LIMIT 1", (master_asin,)
        ).fetchone()
        if not p:
            return out
        out["product"] = dict(p)
        out["category_path"] = p["category_path"] or ""
        product_id = p["id"]

        detail = conn.execute(
            """SELECT html_content FROM detail_pages
               WHERE product_id=? ORDER BY updated_at DESC LIMIT 1""",
            (product_id,),
        ).fetchone()
        if detail and detail["html_content"]:
            out["detail_html"] = detail["html_content"]

        # smartstore listing 의 카테고리/coupang_category_code 둘 다 사용
        ls = conn.execute(
            """SELECT channel, coupang_category_code FROM listings_pa
               WHERE product_id=?""", (product_id,)
        ).fetchall()
        for l in ls:
            if l["channel"] == "coupang" and l["coupang_category_code"]:
                out["coupang_category_code"] = l["coupang_category_code"]

        # images: image_cache 에서 master 의 public_url
        from backend.purchase.services.coupang_lister import _get_product_images as _coupang_images
        try:
            out["image_urls"] = _coupang_images(product_id) or []
        except Exception:
            out["image_urls"] = []
    return out


# ── 네이버 페이로드 ───────────────────────────────────
def build_smartstore_payload(
    group: dict, split: dict, pricing_for_split: list[dict]
) -> Optional[dict]:
    """multi-option 네이버 originProduct 페이로드.

    pricing_for_split 의 sale_krw 들 중 MIN 이 base_price, 각 옵션 delta 로.
    """
    options = split.get("options") or []
    if not options:
        return None
    # master = split 내 category_path 보유 child 우선, 없으면 첫 product 보유 child
    master = None
    meta = None
    product = None
    for o in options:
        m = _load_master_meta(o.get("asin"))
        if m.get("product") and m.get("category_path"):
            master = o; meta = m; product = m["product"]; break
    if not master:
        for o in options:
            m = _load_master_meta(o.get("asin"))
            if m.get("product"):
                master = o; meta = m; product = m["product"]; break
    if not master:
        logger.warning(f"[group-lister] split '{split.get('name')}' — products 테이블에 children 0건")
        return None
    if not product:
        logger.warning(f"[group-lister-smartstore] master {master.get('asin')} products 없음")
        return None

    name = (split.get("name") or product.get("title_ko") or product.get("title_en") or "").strip()
    if len(name) > 80:
        name = name[:80].rstrip()   # 네이버 100자 hard, 80자 권장 (검색 노출)
    category = product.get("category_path") or ""
    if not name or not category:
        return None

    # pricing 매핑 (asin → sale_krw)
    by_asin = {p["child_asin"]: p for p in pricing_for_split}
    valid = [(o, by_asin.get(o.get("asin"))) for o in options if by_asin.get(o.get("asin"))]
    if not valid:
        return None
    prices = [p["sale_krw"] for _, p in valid]
    base_price = min(prices)

    dim_groups = _extract_option_values(split)
    if not dim_groups:
        # 옵션 차원 정보가 없으면 multi-option 의미 없음 — 단일 등록은 별도 함수
        return None

    # C1 fallback 용 master facts (smartstore 도 child 빈 값 채우기)
    ss_master_facts = None
    for c in group.get("children") or []:
        if c.get("asin") == master.get("asin"):
            ss_master_facts = c
            break

    option_simple = []
    for dg in dim_groups:
        for v in dg["values"]:
            option_simple.append({"groupName": dg["groupName"], "name": (v or "")[:24]})   # 네이버 25자 제한

    option_combinations = []
    for i, (child, pr) in enumerate(valid, 1):
        names = []
        for dg in dim_groups:
            raw = child.get(dg["key"])
            # C1 fallback: child 비면 master facts 재사용
            if not raw and ss_master_facts:
                raw = ss_master_facts.get(dg["key"])
            if not raw:
                names.append("")
                continue
            from backend.purchase.services.variation import korean_label
            names.append(korean_label(raw) or raw)
        # 네이버는 신규 등록 시 id 자동발번 — 클라이언트가 보내면 거부됨
        oc = {"stockQuantity": 100,
              "price": pr["sale_krw"] - base_price,
              "sellerManagerCode": child.get("asin") or ""}
        for j, nm in enumerate(names[:4]):
            oc[f"optionName{j + 1}"] = (nm or "")[:24]   # 네이버 25자 제한
        option_combinations.append(oc)

    # 네이버 이미지: image_cache → naver_cdn_url 만 허용 (Amazon URL 직접 거부됨)
    # 비어있으면 download_product_images 로 image_cache row 생성 → 네이버 CDN 업로드
    master_product_id = (meta.get("product") or {}).get("id")
    image_urls = []
    if master_product_id:
        from backend.purchase.services.smartstore_lister import _get_product_images as _ss_get_images
        image_urls = _ss_get_images(master_product_id) or []
        if not image_urls:
            master_facts = None
            for c in group.get("children") or []:
                if c.get("asin") == master.get("asin"):
                    master_facts = c
                    break
            if master_facts and master_facts.get("images"):
                try:
                    import asyncio
                    from backend.purchase.services.image_downloader import download_product_images
                    facts_imgs_json = json.dumps(master_facts["images"])
                    asyncio.run(download_product_images(master_product_id, facts_imgs_json))
                    image_urls = _ss_get_images(master_product_id) or []
                except Exception as e:
                    logger.warning(f"[smartstore-image-prep] product {master_product_id}: {e}")
    if not image_urls:
        logger.warning(f"[smartstore] master {master.get('asin')} 이미지 미보유 — 등록 불가")
        return None
    images_payload = {"representativeImage": {"url": image_urls[0]}}
    if len(image_urls) > 1:
        images_payload["optionalImages"] = [{"url": u} for u in image_urls[1:9]]

    detail_html = meta["detail_html"]
    if not detail_html:
        # 같은 group 의 다른 child detail_pages fallback (백필 row 는 detail 미생성)
        from backend.purchase.database import get_db
        asins_in_group = [c.get("asin") for c in group.get("children") or [] if c.get("asin")]
        if asins_in_group:
            ph = ",".join("?" * len(asins_in_group))
            with get_db() as conn:
                row = conn.execute(
                    f"""SELECT dp.html_content FROM detail_pages dp
                        JOIN products p ON p.id = dp.product_id
                        WHERE p.asin IN ({ph}) AND dp.html_content IS NOT NULL AND dp.html_content != ''
                        ORDER BY dp.updated_at DESC LIMIT 1""",
                    asins_in_group,
                ).fetchone()
            if row and row["html_content"]:
                detail_html = row["html_content"]
    if not detail_html:
        detail_html = (f'<div style="text-align:center;padding:40px;font-family:sans-serif">'
                       f'<h2>{name}</h2><p>{(group.get("brand") or "")}</p></div>')

    # detail_html 안의 로컬 이미지 src 를 네이버 CDN URL 로 1:1 치환 (smartstore_lister 동일 패턴)
    if detail_html and image_urls:
        import re as _re
        _local_pattern = _re.compile(r'(?:http://[^"]*)?/api/pa/images/products/\d+/img_\d+\.jpg')
        for idx, local_url in enumerate(_local_pattern.findall(detail_html)):
            replacement = image_urls[idx] if idx < len(image_urls) else (image_urls[0] if image_urls else "")
            detail_html = detail_html.replace(local_url, replacement)

    detail_attribute = {
        "naverShoppingSearchInfo": {
            "modelName": name[:50],
            "manufacturerName": (group.get("brand") or "")[:50],
            "brandName": (group.get("brand") or "")[:50],
            "catalogMatchingYn": False,
        },
        "afterServiceInfo": {
            "afterServiceTelephoneNumber": "010-8558-7277",
            "afterServiceGuideContent": "해외 구매대행 상품으로 국내 A/S가 불가합니다. 네이버 톡톡 또는 1:1 문의를 이용해주세요.",
        },
        "originAreaInfo": {
            "originAreaCode": "03",
            "content": "상세페이지 참고",
            "importer": "Charis G",
        },
        "taxType": "TAX",
        "minorPurchasable": True,
        "customsTaxType": "EXCLUDED",
        # 인증 면제 — 해외 구매대행 (어린이제품/KC/친환경 카테고리 등록 시 필수)
        "certificationTargetExcludeContent": {
            "childCertifiedProductExclusionYn": True,
            "kcCertifiedProductExclusionYn": "KC_EXEMPTION_OBJECT",
            "kcExemptionType": "OVERSEAS",
            "greenCertifiedProductExclusionYn": True,
        },
        "productInfoProvidedNotice": {
            "productInfoProvidedNoticeType": "ETC",
            "etc": {
                "returnCostReason": "네이버 톡톡 또는 1:1 문의",
                "noRefundReason": "네이버 톡톡 또는 1:1 문의",
                "qualityAssuranceStandard": "제조사/수입사 품질보증 기준에 따름",
                "compensationProcedure": "전자상거래 등에서의 소비자보호에 관한 법률에 따름",
                "troubleShootingContents": "네이버 톡톡 또는 1:1 문의",
                "itemName": name[:50],
                "modelName": name[:50],
                "manufacturer": (group.get("brand") or "")[:50],
                "customerServicePhoneNumber": "010-8558-7277",
            },
        },
        "optionInfo": {
            "simpleOptionSortType": "CREATE",
            "optionSimple": option_simple,
            "optionCombinationSortType": "CREATE",
            "optionCombinations": option_combinations,
            "useStockManagement": True,
        },
    }

    return {
        "originProduct": {
            "statusType": "SALE",
            "name": name,
            "salePrice": base_price,
            "stockQuantity": 100 * len(valid),
            "leafCategoryId": category,
            "detailContent": detail_html,
            "images": images_payload,
            "deliveryInfo": {
                "deliveryType": "DELIVERY",
                "deliveryAttributeType": "NORMAL",
                "deliveryCompany": "CJGLS",
                "deliveryBundleGroupUsable": True,
                "deliveryBundleGroupId": 57248768,
                "deliveryFee": {"deliveryFeeType": "FREE"},
                "claimDeliveryInfo": {
                    "returnDeliveryCompanyPriorityType": "PRIMARY",
                    "returnDeliveryFee": 5000,
                    "exchangeDeliveryFee": 5000,
                    "shippingAddressId": 200297709,
                    "returnAddressId": 200335116,
                    "freeReturnInsuranceYn": False,
                },
            },
            "detailAttribute": detail_attribute,
        },
        "smartstoreChannelProduct": {
            "channelProductDisplayStatusType": "ON",
            "naverShoppingRegistration": True,
        },
    }


# ── 쿠팡 페이로드 ─────────────────────────────────────
def build_coupang_payload(
    group: dict, split: dict, pricing_for_split: list[dict]
) -> Optional[dict]:
    """multi-option 쿠팡 sellerProducts 페이로드.

    items 배열 N개 — 각 child 가 1개 vendorItem.
    """
    options = split.get("options") or []
    if not options:
        return None
    # master = split 내 options 중 coupang_category_code 보유 child 우선, 없으면 첫 child
    master = None
    meta = None
    product = None
    for o in options:
        m = _load_master_meta(o.get("asin"))
        if m.get("product") and m.get("coupang_category_code"):
            master = o; meta = m; product = m["product"]; break
    if not master:
        for o in options:
            m = _load_master_meta(o.get("asin"))
            if m.get("product"):
                master = o; meta = m; product = m["product"]; break
    if not master:
        logger.warning(f"[group-lister] split '{split.get('name')}' — products 테이블에 children 0건")
        return None
    if not product:
        logger.warning(f"[group-lister-coupang] master {master_asin} products 없음")
        return None

    name = (split.get("name") or product.get("title_ko") or product.get("title_en") or "").strip()
    if len(name) > 80:
        name = name[:80].rstrip()   # 쿠팡 sellerProductName 100자, 70~80자 권장
    if not name:
        return None
    category_code = meta.get("coupang_category_code")
    category = str(category_code) if category_code else "0"   # 자동매칭 fallback

    by_asin = {p["child_asin"]: p for p in pricing_for_split}
    valid = [(o, by_asin.get(o.get("asin"))) for o in options if by_asin.get(o.get("asin"))]
    if not valid:
        return None

    # 1차: image_cache 자체 호스트 URL (쿠팡이 detail 검증 시 사이즈 통과하는 리사이즈된 이미지)
    master_product_id = (meta.get("product") or {}).get("id")
    image_urls = meta["image_urls"]
    if not image_urls and master_product_id:
        master_facts = None
        for c in group.get("children") or []:
            if c.get("asin") == master.get("asin"):
                master_facts = c
                break
        if master_facts and master_facts.get("images"):
            try:
                import asyncio
                from backend.purchase.services.image_downloader import download_product_images
                from backend.purchase.services.coupang_lister import _get_product_images as _cp_get_images
                asyncio.run(download_product_images(master_product_id, json.dumps(master_facts["images"])))
                image_urls = _cp_get_images(master_product_id) or []
            except Exception as e:
                logger.warning(f"[coupang-image-prep] product {master_product_id}: {e}")
    # 2차 fallback: facts.images Amazon URL (쿠팡 검수 거부 위험 — 최후 수단)
    if not image_urls:
        for c in group.get("children") or []:
            if c.get("asin") == master.get("asin") and c.get("images"):
                image_urls = c["images"][:9]
                break
    if not image_urls:
        image_urls = []

    # 카테고리 메타 / MANDATORY 속성 — 채널 등록 시 필요
    from backend.purchase.services.coupang_meta import get_category_meta, build_default_notices, get_required_attributes
    meta_cat = get_category_meta(category) if category != "0" else None
    required_attrs = get_required_attributes(category) if category != "0" else []
    required_names = {a.get("attributeTypeName") for a in required_attrs if a.get("attributeTypeName")}
    # AI 매핑 캐시 (그룹 단위) — master facts 기반 mandatory 자동 채움
    master_facts = None
    for c in group.get("children") or []:
        if c.get("asin") == master.get("asin"):
            master_facts = c
            break
    ai_mandatory = _get_or_compute_mandatory_attrs(group.get("parent_asin"), master_facts or {}, category)

    dim_groups = _extract_option_values(split)
    if not dim_groups:
        return None

    items = []
    now = datetime.now(timezone.utc)
    sale_started_at = now.strftime("%Y-%m-%dT%H:%M:%S")
    sale_ended_at = (now + timedelta(days=365 * 5)).strftime("%Y-%m-%dT%H:%M:%S")

    from backend.purchase.services.coupang_lister import STATIC_BANNER_PATHS
    from backend_shared._config import PUBLIC_BASE_URL
    _banner_base = (PUBLIC_BASE_URL or "").rstrip("/")
    _banner_contents = [
        {"contentsType": "IMAGE_NO_SPACE",
         "contentDetails": [{"content": f"{_banner_base}{rel}", "detailType": "IMAGE", "altText": ""}]}
        for rel in STATIC_BANNER_PATHS
    ] if _banner_base else []

    for idx, (child, pr) in enumerate(valid):
        attributes = []
        from backend.purchase.services.variation import korean_label
        provided_names = set()

        def _default_for(name: str) -> str:
            if "스타일" in name: return "기본"
            if "사이즈" in name or "크기" in name: return "원사이즈"
            if "색상" in name: return "기타"
            if "맛" in name or "향" in name: return "기본"
            return "기본"

        for dg in dim_groups:
            raw = child.get(dg["key"])
            # C1 fallback: child 비면 master 의 같은 attribute (color, size 등) 재사용
            if not raw and master_facts:
                raw = master_facts.get(dg["key"])
            val = (korean_label(raw) or raw) if raw else None
            if not val:
                if dg["groupName"] in required_names:
                    val = _default_for(dg["groupName"])
                else:
                    continue
            attributes.append({
                "attributeTypeName": dg["groupName"],
                "attributeValueName": val[:24],
                "exposed": "EXPOSED",
                "editable": True,
            })
            provided_names.add(dg["groupName"])

        # 카테고리 mandatory 자동 채움 — AI 매핑 결과 사용 (캐시)
        for ra in required_attrs:
            tname = ra.get("attributeTypeName") or ""
            if not tname or tname in provided_names:
                continue
            ai_v = (ai_mandatory or {}).get(tname)
            if not ai_v:
                continue
            raw_val = ai_v.get("value") if isinstance(ai_v, dict) else ai_v
            if raw_val is None or raw_val == "":
                continue
            unit = (ai_v.get("unit") if isinstance(ai_v, dict) else "") or ""
            value_str = (f"{raw_val} {unit}".strip() if unit else str(raw_val)).strip()[:24]
            attributes.append({
                "attributeTypeName": tname,
                "attributeValueName": value_str,
                "exposed": "EXPOSED",
                "editable": True,
            })

        item_name = _option_label_for_child(child, dim_groups)
        # 각 child 의 facts.images 우선 (옵션별 이미지) → master image_urls fallback
        # 모든 옵션이 master image_urls (자체 호스트, 리사이즈 통과) 공유.
        # child.images Amazon URL 사용 시 쿠팡 detail 검증에서 size 거부.
        per_item_urls = image_urls[:9]
        item_images = []
        for i, url in enumerate(per_item_urls):
            item_images.append({
                "imageOrder": i,
                "imageType": "REPRESENTATION" if i == 0 else "DETAIL",
                "vendorPath": url,
            })

        item_contents = [
            {"contentsType": "IMAGE_NO_SPACE",
             "contentDetails": [{"content": u, "detailType": "IMAGE", "altText": ""}]}
            for u in per_item_urls[:10]
        ] + _banner_contents
        items.append({
            "itemName": item_name,
            "originalPrice": int(pr["sale_krw"] * 1.2),
            "salePrice": int(pr["sale_krw"]),
            "maximumBuyCount": 100,
            "maximumBuyForPerson": 0,
            "maximumBuyForPersonPeriod": 1,
            "outboundShippingTimeDay": 7,
            "unitCount": 1,
            "adultOnly": "EVERYONE",
            "taxType": "TAX",
            "parallelImported": "NOT_PARALLEL_IMPORTED",
            "overseasPurchased": "OVERSEAS_PURCHASED",
            "pccNeeded": True,
            "externalVendorSku": child.get("asin"),
            "barcode": "",
            "emptyBarcode": True,
            "emptyBarcodeReason": "COUPANG",
            "modelNo": item_name[:50],
            "extraProperties": {},
            "certifications": [],
            "searchTags": [],
            "offerCondition": "NEW",
            "stockQuantity": 100,
            "saleStartedAt": sale_started_at,
            "saleEndedAt": sale_ended_at,
            "displayProductName": item_name,
            "brand": group.get("brand") or "",
            "manufacture": group.get("brand") or "",
            "images": item_images,
            "attributes": attributes,
            "notices": build_default_notices(meta_cat) if meta_cat else [],
            "contents": item_contents,
        })

    from backend_shared._config import (
        COUPANG_VENDOR_ID, COUPANG_USER_ID,
        COUPANG_OUTBOUND_SHIPPING_PLACE_CODE, COUPANG_RETURN_CENTER_CODE,
    )
    return {
        "sellerProductName": name,
        "displayCategoryCode": int(category) if category.isdigit() else 0,
        "vendorId": COUPANG_VENDOR_ID,
        "saleStartedAt": sale_started_at,
        "saleEndedAt": sale_ended_at,
        "displayProductName": name,
        "brand": group.get("brand") or "",
        "manufacture": group.get("brand") or "",
        "deliveryMethod": "AGENT_BUY",
        "deliveryCompanyCode": "CJGLS",
        "deliveryChargeType": "FREE",
        "deliveryCharge": 0,
        "freeShipOverAmount": 0,
        "deliveryChargeOnReturn": 9000,
        "remoteAreaDeliverable": "N",
        "unionDeliveryType": "NOT_UNION_DELIVERY",
        "returnCenterCode": COUPANG_RETURN_CENTER_CODE,
        "returnChargeName": "Charis G",
        "companyContactNumber": "010-8558-7277",
        "returnZipCode": "01425",
        "returnAddress": "서울특별시 도봉구 해등로 24",
        "returnAddressDetail": "(반품 받는 주소 — 추후 환경별 보정)",
        "returnCharge": 9000,
        "outboundShippingPlaceCode": int(COUPANG_OUTBOUND_SHIPPING_PLACE_CODE) if COUPANG_OUTBOUND_SHIPPING_PLACE_CODE else 0,
        "vendorUserId": COUPANG_USER_ID or COUPANG_VENDOR_ID,
        "requested": True,   # 등록과 동시에 승인 요청 (단일 등록과 동일)
        "items": items,
        "requiredDocumentNames": [],
        "extraInfoMessage": "",
        "manufacture": group.get("brand") or "",
    }


# ── register_group_listings — 채널 등록 + DB 저장 ────
def register_group_listings(parent_asin: str, channels: list[str] | None = None) -> dict:
    """한 group 의 분리 결과를 채널에 등록.

    동작:
      1. variation.load_group(parent_asin)
      2. for ch in channels:
         - variation.auto_split(group, ch) → splits 리스트
         - 각 split:
           - variation.calculate_group_pricing(...)
           - build_smartstore_payload / build_coupang_payload
           - register_product (smartstore_lister / coupang_lister)
           - listings_pa INSERT / UPDATE (channel_product_id)
           - listing_options INSERT (각 child = 1 옵션)

    반환: {channel: {ok: N, fail: M, listings: [...]}}
    """
    from backend.purchase.services.variation import (
        load_group, auto_split, calculate_group_pricing,
    )

    channels = channels or ["smartstore", "coupang"]
    group = load_group(parent_asin)
    if not group:
        return {"error": f"group {parent_asin} 없음"}

    out = {}
    for ch in channels:
        splits = auto_split(group, ch)
        ch_result = {"ok": 0, "fail": 0, "skipped": 0, "listings": [], "errors": []}
        pricing_full = calculate_group_pricing(group, ch)
        pricing_by_asin = {p["child_asin"]: p for p in pricing_full}

        for split in splits:
            opt_asins = [o.get("asin") for o in split.get("options") or []]
            split_pricing = [pricing_by_asin[a] for a in opt_asins if a in pricing_by_asin]
            if not split_pricing:
                ch_result["skipped"] += 1
                continue
            try:
                if ch == "smartstore":
                    payload = build_smartstore_payload(group, split, split_pricing)
                else:
                    payload = build_coupang_payload(group, split, split_pricing)
            except Exception as e:
                ch_result["fail"] += 1
                ch_result["errors"].append({"split_name": split.get("name"), "error": str(e)[:300]})
                continue

            if not payload:
                ch_result["skipped"] += 1
                continue

            # 실제 채널 호출은 여기서 (별도 검증 단계에서 활성화)
            ch_result["listings"].append({
                "name": split.get("name"),
                "options_count": len(split_pricing),
                "payload_keys": list(payload.keys()),
                "_dry_run": True,   # Phase 3-D/E 코어 — 실제 등록은 register_product 호출 추가 시
            })
            ch_result["ok"] += 1

        out[ch] = ch_result
    return out
