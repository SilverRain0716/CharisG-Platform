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


def _gen_group_shared_editorial(pid):
    """그룹 공유 텍스트 에디토리얼(사진無) 1회 생성 → ed_shared.json URL 리스트. 실패시 []. (2026-07-05)"""
    import subprocess as _sp, sys as _sy, os as _os, json as _sj
    from pathlib import Path as _P
    _rb = _P.home() / "CharisG-Platform/charisg-platform"
    _shf = _rb / "backend/purchase/media/products" / str(pid) / "ed_shared.json"
    try:
        if not _shf.exists():
            _sp.run([_sy.executable, str(_rb / "scripts/migrate/render_editorial_runner.py"), str(pid), "textonly"],
                    cwd=str(_rb), env={**_os.environ, "PYTHONPATH": str(_rb)}, timeout=180, capture_output=True, text=True)
        if _shf.exists():
            return _sj.loads(_shf.read_text()) or []
    except Exception as _e:
        logger.warning(f"[group-shared-ed] {pid} 실패: {_e}")
    return []


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
def _naver_extend_with_options(origin_no: str, options_simple: list, options_combinations: list,
                              base_price: int | None = None,
                              group_names: dict | None = None) -> Optional[dict]:
    """기존 originProduct 의 detailAttribute.optionInfo 에 options 셋팅 후 PUT.

    내부적으로 update_product (GET → merge → PUT + 금지태그 자동 strip) 활용.
    """
    from backend.purchase.services.naver_commerce_service import get_product, update_product
    current = get_product(str(origin_no))
    if not current:
        logger.warning(f"[naver-extend] {origin_no} 조회 실패")
        return None
    detail = (current.get("originProduct") or {}).get("detailAttribute") or {}
    # ★2026-08-08: 조합형 우선. optionCombinationGroupNames 가 없으면 네이버가
    #   optionCombinations 를 통째로 버리고, optionSimple 과 병행하면 단독형이 이긴다.
    #   단독형에는 sellerManagerCode(자식 ASIN) 자리가 없어 주문 역추적이 불가능하다.
    if options_combinations and group_names:
        detail["optionInfo"] = {
            "optionCombinationSortType": "CREATE",
            "optionCombinationGroupNames": group_names,
            "optionCombinations": options_combinations,
            "optionSimple": [],
            "useStockManagement": True,
        }
    else:
        # 조합 정보가 없으면 종전 단독형 (옵션 축 정보를 못 만든 경우의 폴백)
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


def verify_naver_option_structure(origin_no: str, expected: int) -> tuple[bool, str]:
    """등록·확장 직후 네이버가 '조합형'으로 저장했는지 되읽어 확인한다.

    ★왜 필요한가 (2026-04-24 사고): update_product 가 200 을 줘도 네이버가 조합형을
      버리고 단독형으로 저장하는 경우가 있다. 단독형에는 sellerManagerCode(자식 ASIN)
      자리가 없어 주문이 와도 어느 옵션인지 역추적할 수 없다. 그때 등록된 16건은
      '성공'으로 기록됐고, 4개월 뒤 정합성 점검에서야 드러났다(그 사이 주문이
      들어왔다면 전부 오배송).

    성공 응답을 믿지 않고 실제 저장 결과를 센다. 반환: (정상 여부, 사유)
    """
    from backend.purchase.services.naver_commerce_service import get_product
    cur = get_product(str(origin_no))
    if not cur:
        return False, "등록 후 조회 실패 — 구조 확인 불가"
    oi = ((cur.get("originProduct") or {}).get("detailAttribute") or {}).get("optionInfo") or {}
    combos = oi.get("optionCombinations") or []
    simple = oi.get("optionSimple") or []
    if not combos and simple:
        return False, (f"네이버가 단독형으로 저장(optionSimple {len(simple)}개) — "
                       f"sellerManagerCode 부재로 주문 역추적 불가. 조합형 재등록 필요")
    if len(combos) != expected:
        return False, (f"옵션 수 불일치: 의도 {expected} vs 채널 {len(combos)} — "
                       f"일부 옵션이 누락된 채 등록됨")
    # sellerManagerCode 가 비면 ASIN 매핑 자리가 없다 — 개수가 맞아도 무의미하다.
    no_code = sum(1 for c in combos if isinstance(c, dict) and not c.get("sellerManagerCode"))
    if no_code:
        return False, f"조합 {no_code}개에 sellerManagerCode 없음 — 자식 ASIN 매핑 불가"
    return True, ""


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
    # ★2026-08-01: 신규 item 이 '자기 카테고리' 기준 notices 를 갖고 오면 마스터 카테고리와
    #   충돌해 400 ("Cannot enter 'N number Options of 1 number Notices of Category Name'").
    #   한 sellerProduct 의 items 는 같은 카테고리이므로 기존 item 의 notices 를 그대로 물려준다.
    import copy as _copy
    _base_notices = None
    for _it in existing_items:
        if isinstance(_it, dict) and _it.get("notices"):
            _base_notices = _it["notices"]
            break
    appended = 0
    for new_it in new_items:
        if not isinstance(new_it, dict):
            continue
        if new_it.get("externalVendorSku") in existing_skus:
            continue
        if _base_notices:
            new_it["notices"] = _copy.deepcopy(_base_notices)
        existing_items.append(new_it)
        appended += 1
    data["items"] = existing_items
    if appended == 0:
        logger.info(f"[coupang-extend] {seller_product_id} 추가할 items 0건 (이미 등록됨)")
        return {"data": seller_product_id, "code": "SUCCESS", "_no_change": True}

    # 쿠팡 seller-products UPDATE: PUT URL 에 ID 포함 안 함, sellerProductId 는 body 필드.
    # data 는 get_seller_product 응답이라 sellerProductId 가 이미 들어있음.
    path = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
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
        # 주의: requests.Response 는 4xx/5xx 에서 bool() False 라 'r is None' 만 None 판정에 사용.
        status = r.status_code if r is not None else "no-resp"
        body = r.text[:300] if r is not None else ""
        logger.error(f"[coupang-extend] {seller_product_id} PUT 실패: {status} {body}")
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
        if not smc:
            continue
        # ★2026-08-08 정정: 종전엔 {ASIN: ASIN} 을 돌려줘 channel_option_id 에
        #   ASIN 이 들어갔다. 네이버가 조합마다 발번하는 id 가 진짜 옵션 식별자이므로
        #   그것을 쓴다. id 가 없으면(구 데이터) ASIN 으로 폴백.
        out[str(smc)] = str(c.get("id") or smc)
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
    # ★ 계정 태깅 — 쿠팡이면 활성계정('old'|'new'), 아니면 'old' 기본
    try:
        from backend.purchase.services.coupang_service import active_account as _aa
        _acct = _aa() if channel == "coupang" else "old"
    except Exception:
        _acct = "old"
    # 2026-08-03: naver_account 가 아예 채워지지 않아 신계정 그룹등록 3건이 NULL 로 남았다.
    #   쿠팡만 계정 태깅하고 네이버는 누락 — 채널별 계정 구분이 불가능해진다.
    _nacct = None
    if channel == "smartstore":
        try:
            from backend.purchase.services.naver_commerce_service import active_account as _na
            _nacct = _na()
        except Exception:
            _nacct = "old"
    with get_db() as conn:
        # listings_pa INSERT (master child 기준)
        cur = conn.execute(
            """INSERT INTO listings_pa
                (product_id, channel, status, sale_krw, cost_krw_snapshot,
                 fee_rate, net_margin_krw, channel_product_id,
                 has_options, last_synced_at, coupang_category_code,
                 smartstore_channel_no, coupang_auto_matched, coupang_account,
                 naver_account, acct_key)
               VALUES (?, ?, 'listed', ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(product_id, channel, acct_key) DO UPDATE SET
                 channel_product_id=excluded.channel_product_id,
                 status='listed',
                 has_options=1,
                 sale_krw=excluded.sale_krw,
                 cost_krw_snapshot=excluded.cost_krw_snapshot,
                 fee_rate=excluded.fee_rate,
                 net_margin_krw=excluded.net_margin_krw,
                 last_synced_at=excluded.last_synced_at,
                 smartstore_channel_no=COALESCE(excluded.smartstore_channel_no,
                                                listings_pa.smartstore_channel_no),
                 coupang_auto_matched=excluded.coupang_auto_matched,
                 coupang_account=excluded.coupang_account,
                 naver_account=excluded.naver_account""",
            (master_child_id, channel, sale_krw, cost_krw, fee_rate,
             net_margin_krw, channel_product_id, ts, coupang_category_code,
             smartstore_channel_no,
             1 if channel == "coupang" else 0, _acct, _nacct,
             (_acct if channel == "coupang" else (_nacct or "")) or ""),
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
        from backend.purchase.services.sp_api_facts import sp_api_retry
        creds = get_credentials()
        api = Products(credentials=creds, marketplace=Marketplaces.US)
        res = sp_api_retry(
            lambda: api.get_item_offers(asin=asin, item_condition="New", customer_type="Consumer"),
            label=f"pricing {asin}",
        )
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


def _get_prices_batch(asins: list) -> dict:
    """getItemOffersBatch로 최대 20개씩 ASIN 가격 일괄조회 → {asin: price}. (2026-07-25 배치발굴)
    우선순위 _get_buybox_or_lowest_price 동일(BuyBox New > Lowest Amazon > Lowest Merchant).
    배치 실패/누락 ASIN은 결과에 없음 → 호출측이 단건 폴백."""
    out = {}
    if not asins:
        return out
    try:
        from sp_api.api import Products
        from sp_api.base import Marketplaces
        from backend.dropshipping.services.amazon_sp_api_service import get_credentials
        from backend.purchase.services.sp_api_facts import sp_api_retry
    except ImportError:
        return out
    import time as _t
    MP = "ATVPDKIKX0DER"
    def _pick(summary):
        for bb in (summary.get("BuyBoxPrices") or []):
            if (bb.get("condition") or "").lower() == "new":
                lp = bb.get("LandedPrice") or {}
                if lp.get("Amount"):
                    return float(lp["Amount"])
        for it in (summary.get("LowestPrices") or []):
            if (it.get("fulfillmentChannel") or "").lower() == "amazon":
                lp = it.get("LandedPrice") or {}
                if lp.get("Amount"):
                    return float(lp["Amount"])
        for it in (summary.get("LowestPrices") or []):
            lp = it.get("LandedPrice") or {}
            if lp.get("Amount"):
                return float(lp["Amount"])
        return None
    try:
        creds = get_credentials()
        api = Products(credentials=creds, marketplace=Marketplaces.US)
        for i in range(0, len(asins), 20):
            batch = asins[i:i + 20]
            reqs = [{"uri": f"/products/pricing/v0/items/{a}/offers", "method": "GET",
                     "MarketplaceId": MP, "ItemCondition": "New", "CustomerType": "Consumer"} for a in batch]
            res = sp_api_retry(lambda: api.get_item_offers_batch(reqs), label=f"pricing_batch({len(batch)})")
            for r in ((res.payload or {}).get("responses") or []):
                if (r.get("status") or {}).get("statusCode") != 200:
                    continue
                pl = (r.get("body") or {}).get("payload") or {}
                a = pl.get("ASIN")
                price = _pick(pl.get("Summary") or {})
                if a and price:
                    out[a] = price
            if i + 20 < len(asins):
                _t.sleep(10)  # getItemOffersBatch 0.1 RPS
    except Exception as e:
        logger.warning(f"[pricing-batch] 실패: {e}")
    return out


def fetch_and_insert_children(parent_asin: str, job_id: Optional[str] = None) -> dict:
    """variation_groups.child_asins 중 products 에 없는 ASIN → SP-API + Pricing API 동시 호출 → INSERT.

    B안 (per-ASIN 병렬): 한 ASIN당 CatalogItems / getItemOffers 두 호출을 ThreadPoolExecutor 로 동시 진행,
    INSERT 시점에 cost_usd 같이 채움 (BuyBox > Lowest > master fallback).
    Pricing API 1 RPS 제한이 bottleneck → ASIN 간엔 sleep(1.0) 유지 (sequential).
    """
    import time as _time
    from concurrent.futures import ThreadPoolExecutor
    from backend.purchase.database import get_db
    from backend.purchase.services.sp_api_facts import fetch_facts_with_raw, _persist_facts, normalize_catalog_item
    from backend.purchase.services.sp_api_group_discovery import catalog_batch

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
        # ★ 기존 row 중 핵심 필드(parent_asin/sp_api_facts) NULL → 보강 대상.
        # 레거시 단일소싱 경로로 적재된 row 는 group 메타 비어있어 variation 옵션화 실패.
        backfill_rows = conn.execute(
            f"""SELECT asin FROM products
                WHERE asin IN ({ph})
                  AND (parent_asin IS NULL OR sp_api_facts_json IS NULL OR sp_raw_json IS NULL)""",
            child_asins,
        ).fetchall()
        master_row = conn.execute(
            "SELECT cost_usd FROM products WHERE asin=? AND cost_usd > 0 LIMIT 1",
            (parent_asin,),
        ).fetchone()
    existing_set = {r["asin"] for r in existing}
    backfill_set = {r["asin"] for r in backfill_rows}
    target_new = [a for a in child_asins if a not in existing_set]
    target_backfill = [a for a in child_asins if a in backfill_set]
    target = target_new + target_backfill
    total = len(target)
    master_cost = float(master_row["cost_usd"]) if master_row else 0.0

    inserted = errors = 0
    cost_buybox = cost_fallback = cost_no_data = 0
    # ★배치 가격(2026-07-25): 자식 전체 가격을 getItemOffersBatch로 선-조회 (per-child 0.5RPS 스로틀 제거)
    _batch_prices = _get_prices_batch(target)
    # ★배치 카탈로그(2026-07-25): 자식 facts를 searchCatalogItems 20/콜로 일괄 조회.
    #   item은 getCatalogItem과 동일구조(검증) → normalize_catalog_item 재사용. 누락은 단건 폴백.
    _batch_items = catalog_batch(target, ["summaries", "attributes", "images",
                                          "dimensions", "productTypes", "identifiers",
                                          "relationships", "salesRanks"])
    for i, asin in enumerate(target, 1):
        try:
            _cit = _batch_items.get(asin)
            if _cit is not None:
                facts, raw_item = normalize_catalog_item(asin, _cit), _cit
            else:
                facts, raw_item = fetch_facts_with_raw(asin)  # 배치 누락 → 단건 폴백
            price = _batch_prices.get(asin)
            if price is None:
                price = _get_buybox_or_lowest_price(asin)  # 배치 누락분만 단건 폴백
            if not facts:
                errors += 1
                _time.sleep(0.5)
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
                if asin in existing_set:
                    # 기존 row 보강 — NULL 만 채움 (COALESCE), title/brand 등 사용자 수정 보존
                    conn.execute(
                        """UPDATE products SET
                              parent_asin = COALESCE(parent_asin, ?),
                              sp_api_facts_json = COALESCE(sp_api_facts_json, ?),
                              sp_api_facts_at = COALESCE(sp_api_facts_at, ?),
                              group_master_asin = COALESCE(group_master_asin, ?),
                              cost_usd = COALESCE(NULLIF(cost_usd, 0), ?)
                           WHERE asin=?""",
                        (
                            parent_asin,
                            json.dumps(facts, ensure_ascii=False),
                            facts.get("fetched_at"),
                            parent_asin,
                            cost_usd,
                            asin,
                        ),
                    )
                else:
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
            # ★전체 SP-API 데이터 저장 (sp_* 컬럼 + sp_raw_json) — INSERT 후 row 존재하므로 persist 가능.
            #   2026-06-30: 자식이 부실데이터(facts_json만)로 옵션화되던 문제 해결. raw 재사용=SP-API 추가호출 0.
            if raw_item is not None:
                try:
                    _persist_facts(asin, facts, raw=raw_item)
                except Exception as _pe:
                    logger.warning(f"[insert-children] persist sp_* 실패 {asin}: {_pe}")
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
        # sleep 제거(2026-07-25): 카탈로그·가격 모두 배치라 per-child 스로틀 불필요
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
    # ★행 존재가 아니라 "현행 템플릿 버전인가" 로 판단(2026-08-07). 종전엔 옛 문구
    #   HTML 이 있으면 그대로 재사용해서 구버전이 신규 등록으로 계속 새어나갔다.
    from backend.purchase.services.ai_processor import detail_is_current
    if not detail_is_current(product_id):
        try:
            # 2026-08-07: backend_shared.detail_page_service(12섹션 SECTION_HTML 엔진)를
            #   쓰던 자리. 그 엔진은 detail_templates(0행)·프론트 Templates.jsx(미존재)
            #   전제라 실제로는 하드코딩 default 섹션만 뱉었고, 결과물이 네이버 실사용
            #   상세(_build_pa_html)와 완전히 달랐다 — 같은 상품이 이 폴백을 타면 혼자
            #   다른 상세를 받았다. 실사용 엔진 한 벌로 통일.
            from backend.purchase.services.ai_processor import ensure_detail_html
            ensure_detail_html(product_id, platform="smartstore")
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
                    # 마진 게이트 (쿠팡만 — net_margin_krw < 15K 차단)
                    from backend.purchase.services.margin_gate import block_listing_if_low_margin
                    blocked, mreason = block_listing_if_low_margin(product_id)
                    if blocked:
                        logger.info(f"[master-singleton] {parent}/{master_asin} 마진 차단: {mreason}")
                        continue
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


def _group_path_blocking_gates(parent_asin: str, channel: str = "coupang") -> Optional[str]:
    """그룹/Retrofit 경로 진입 차단 게이트 — 단일 경로(coupang_lister) 와 동일 정책 미러.

    계정 품질·정지 예방을 위해 단일 상품에서 차단되던 정책이 그룹 경로(키워드/시트→옵션묶기)
    에서도 우회되지 않도록 보장. 차단 시 reason 반환 + log_violation, 통과 시 None.

    체크 항목:
      1. 한국 제조사 (해외 직배 정책)
      2. 금지 성분 (식약처/약사법)
      3. DTC 유전자검사 키트 (생명윤리법 제49조1항 영구차단)
      4. 의류·신발 (사장님 지시 임시차단, PA_DISABLE_APPAREL_SHOES_BLOCK=1 해제)
      5. KC 비면제 품목 (KC마크 없이 구매대행 불가)
    """
    from backend.purchase.services import clean_policy
    from backend.purchase.database import get_db

    # ★신 파이프라인 리스크 3축 (2026-08-12 추가)
    #   기존 5항목은 ASIN 블랙리스트·키워드다. M12~M14 가 잡는 **미조사 S등급 브랜드**는
    #   여기서 안 걸러진다. import_risk 판정이 있으면 그것을 우선한다.
    #   ★판정 자체가 없으면 막지 않는다 — 구 파이프라인 상품은 import_risk 가 없다.
    try:
        with get_db() as _c:
            _rk = _c.execute(
                """SELECT r.axis, r.verdict, r.reason
                     FROM import_risk r
                     JOIN import_detail d ON d.batch=r.batch AND d.asin=r.asin
                    WHERE d.parent_asin=? AND r.verdict IN ('차단','대상','보류','사람검토')
                    LIMIT 1""", (parent_asin,)).fetchone()
        if _rk:
            return "import_risk %s=%s: %s" % (_rk["axis"], _rk["verdict"], (_rk["reason"] or "")[:80])
    except Exception as _e:  # noqa: BLE001
        logger.warning(f"[gate] import_risk 조회 실패 {parent_asin}: {str(_e)[:80]}")

    with get_db() as conn:
        # 자식 전체 조회 — title 애그리게이션용 (parent title_ko 부실 케이스 대비, 2026-07-18)
        _all_kids = conn.execute(
            "SELECT id, title_en, title_ko, brand, category_path, amazon_manufacturer, "
            "sp_manufacturer FROM products WHERE parent_asin=? ORDER BY id",
            (parent_asin,),
        ).fetchall()
        if not _all_kids:
            return None
        p = _all_kids[0]
        cat_row = conn.execute(
            "SELECT coupang_category_code FROM listings_pa WHERE product_id=? AND channel='coupang' LIMIT 1",
            (p["id"],),
        ).fetchone()
    title_en = p["title_en"] or ""
    title_ko = p["title_ko"] or ""
    brand = p["brand"]
    cat_code = cat_row["coupang_category_code"] if cat_row else None
    # 자식 전체 title/category 애그리게이션 (의류 게이트 안전망, 2026-07-18)
    _title_ko_agg = " | ".join((k["title_ko"] or "") for k in _all_kids).strip(" |")
    _title_en_agg = " | ".join((k["title_en"] or "") for k in _all_kids).strip(" |")
    _cat_path_agg = " | ".join((k["category_path"] or "") for k in _all_kids).strip(" |")
    stage = f"register_group_{channel}"

    def _block(vtype: str, kw: str, reason: str) -> str:
        try:
            clean_policy.log_violation(
                stage=stage, violation_type=vtype, action_taken='blocked',
                matched_keyword=kw, asin=parent_asin, channel=channel,
                original_text=title_en or title_ko,
            )
        except Exception:
            pass
        return reason

    # ★관세 $150 사전차단 (2026-07-12): 자식 전부 원가>$150 이면 등록 가능한 게 없음 → 무거운 prep 전 탈출.
    #   일부라도 <=$150(또는 cost 불명)이면 통과 — 그건 등록직전 list_product 관세게이트가 개별 처리.
    try:
        from backend.purchase.services.coupang_lister import CUSTOMS_DUTY_FREE_USD as _CDF
        with get_db() as _cc:
            _crow = _cc.execute(
                "SELECT SUM(CASE WHEN cost_usd IS NULL OR cost_usd <= ? THEN 1 ELSE 0 END) listable, "
                "MIN(cost_usd) mn FROM products WHERE parent_asin=?",
                (_CDF, parent_asin),
            ).fetchone()
        if _crow and (_crow["listable"] or 0) == 0 and _crow["mn"] is not None:
            return _block("customs_over_limit", f"${_crow['mn']:.0f}",
                          f"관세 한도 초과 — 전 자식 원가 > ${int(_CDF)} (목록통관 면세한도)")
    except Exception:
        pass

    # 리콜 차단 (2026-07-08) — 그룹 자식 중 리콜품 있으면 그룹 전체 차단
    try:
        from backend.purchase.services.recall_blocklist import is_recalled
        with get_db() as _rc:
            _kids = _rc.execute("SELECT asin, title_en, title_ko FROM products WHERE parent_asin=?", (parent_asin,)).fetchall()
        for _k in _kids:
            _rr = is_recalled(_k["asin"], _k["title_en"] or _k["title_ko"])
            if _rr:
                return _block('recalled', _k["asin"] or '', f"리콜 상품 포함 ({_rr})")
    except Exception:
        pass
    # 브랜드 블랙리스트 (단일 경로 미러 — 정품 게이팅 브랜드 선제 차단, 2026-06-09)
    try:
        from backend.purchase.services.coupang_lister import _is_brand_blocked, _load_brand_blocklist
        _bm = _is_brand_blocked(title_en, title_ko, _load_brand_blocklist())
        # ★삭제이력 ASIN 재등록 차단(2026-08-05)
        if not _bm:
            try:
                from backend.purchase.services import clean_policy as _cp3
                _ab3, _ar3 = _cp3.check_blocked_asin(
                    (product or {}).get("asin") if isinstance(product, dict) else "")
                if _ab3:
                    _bm = _ar3
            except Exception:
                pass

        # ★브랜드필드 차단(2026-08-05) — 단일 경로와 동일 정책
        if not _bm:
            try:
                from backend.purchase.services import clean_policy as _cp
                _b2, _r2 = _cp.check_brand_field_blocked(
                    (product or {}).get("brand") if isinstance(product, dict) else "")
                if _b2:
                    _bm = _r2
            except Exception:
                pass
        if _bm:
            return _block('brand_blocklist', _bm, f"브랜드 블랙리스트 차단 ({_bm})")
    except Exception:
        pass
    # ★2026-08-05 정정: brand 를 넘기고 있었다. 이 함수는 제조사 기준이라 캐시가 계속 빗나갔다.
    _mfr = ((p["amazon_manufacturer"] or "").strip()
            or (p["sp_manufacturer"] or "").strip() or None)
    b, msg = clean_policy.check_korean_manufacturer(_mfr)
    if b:
        return _block('korean_manufacturer', msg or 'unknown', f"한국 제조사 ({msg})")
    b, kw = clean_policy.check_prohibited_ingredients(title_en, title_ko)
    if b:
        return _block('prohibited_ingredient', kw, f"금지 성분 ({kw})")
    b, kw = clean_policy.check_prohibited_genetic_kit(title_en, title_ko)
    if b:
        return _block('dtc_genetic_kit', kw, f"DTC 유전자검사 키트 ({kw}) — 생명윤리법 제49조1항")
    b, op_reason = clean_policy.check_optical_medical_device(title_en, title_ko)
    if b:
        return _block('optical_medical_device', op_reason or 'unknown', f"광학 의료기기 ({op_reason}) — 의료기기법")
    # 의류·신발 게이트 (2026-07-18 강화): 자식 애그리게이션 title + 카테고리 경로 (쿠팡+아마존)
    _apparel_cat = _cat_path_agg
    if cat_code:
        try:
            with get_db() as _cc2:
                _cp = _cc2.execute(
                    "SELECT path FROM coupang_categories WHERE code=? LIMIT 1", (cat_code,)
                ).fetchone()
                if _cp and _cp["path"]:
                    _apparel_cat = (_apparel_cat + " | " + _cp["path"]).strip(" |")
        except Exception:
            pass
    b, kw = clean_policy.check_blocked_apparel_shoes(
        _title_ko_agg or title_ko, _title_en_agg or title_en, _apparel_cat,
    )
    if b:
        return _block('apparel_shoes_blocked', kw, f"의류·신발 임시 차단 ({kw})")
    b, el_kw = clean_policy.check_electric_appliance(title_en, title_ko)
    if b:
        return _block('electric_appliance', el_kw, f"전기용품 ({el_kw}) — KC 전기안전인증")
    b, ec_kw = clean_policy.check_excluded_amazon_category(parent_asin=parent_asin)
    if b:
        return _block('excluded_category', ec_kw, f"취급제외 카테고리 ({ec_kw}) — 거울/벽걸이")
    b, kc_reason = clean_policy.check_kc_blocked(title_en, title_ko, coupang_category_code=cat_code,
                                                brand=brand or "", asin=parent_asin or "")
    if b:
        return _block('kc_required', kc_reason or 'unknown', f"KC 비면제 ({kc_reason})")
    # 의약외품 (약사법) — 단일 경로 미러 (탐폰/생리대/염모제/콘돔/보건용마스크). 2026-06-20 그룹게이트 추가.
    b, qd_kw = clean_policy.check_quasi_drug(title_ko, title_en)
    if b:
        return _block('quasi_drug', qd_kw or 'unknown', f"의약외품 ({qd_kw}) — 약사법")
    # RTD 음료 제외 (마시는 음료 — 파우더/믹스/캡슐은 통과). 2026-06-20 그룹게이트 추가.
    _cat_path = ""
    if cat_code:
        with get_db() as _c:
            _r = _c.execute("SELECT path FROM coupang_categories WHERE code=? LIMIT 1", (cat_code,)).fetchone()
            _cat_path = _r["path"] if _r else ""
    b, bev_r = clean_policy.check_beverage(title_ko or title_en, _cat_path)
    if b:
        return _block('beverage_rtd', bev_r or 'unknown', f"RTD 음료 제외 ({bev_r})")
    return None


def retrofit_extend_with_rebuild(parent_asin: str, seller_product_id: str,
                                  dry_run: bool = True, requested: bool = False) -> dict:
    """기존 라이브 master listing 에 그룹의 풀 items[] 를 신규 파이프라인으로 빌드해 PUT 교체.

    4월 등록 단일들의 빈/generic attrs 가 옵션 구분 실패 원인이었음 → resync + AI detailing +
    build_coupang_payload(새 파이프라인) 로 items[] 전체 재구성 → 기존 master 의 items 만 교체 PUT.
    sellerProductName 도 title_ko(AI 결과) 로 갈아끼움(한글화).

    dry_run=True: 빌드+계획만, PUT 안 함.
    requested=False(기본): 임시저장 PUT (안전, 셀러센터 검토 후 승인 가능).
    """
    import json as _json
    from backend.purchase.services.variation import load_group, auto_split, calculate_group_pricing
    from backend.purchase.services.coupang_service import (
        get_seller_product, _signature, BASE, _request_with_retry,
    )

    out: dict = {"parent_asin": parent_asin, "seller_product_id": seller_product_id,
                 "dry_run": dry_run, "requested": requested}

    # ★ 차단 게이트 (retrofit 도 그룹 경로 — 단일 경로 모든 정책 미러)
    _block = _group_path_blocking_gates(parent_asin, channel="coupang")
    if _block:
        out["error"] = f"blocked: {_block}"
        return out

    # 1) 정합성 + 누락 형제 ingest
    err = resync_group_from_spapi(parent_asin)
    if err:
        out["error"] = f"resync: {err}"
        return out
    out["resync"] = "ok"

    # 2) AI detailing — title_ko/desc_ko/seo 채움 (sellerProductName 한글화 위해).
    # dry_run 에선 비용 큰 batch 스킵(빌드 검증 목적). 라이브에선 실행.
    if not dry_run:
        try:
            from backend.purchase.services.keyword_to_groups import _run_ai_detailing_for_group
            n_det = _run_ai_detailing_for_group(parent_asin, platform="coupang")
            out["ai_detailed"] = n_det
        except Exception as e:
            logger.warning(f"[retrofit] {parent_asin} AI detailing 부분 실패(계속): {e}")
            out["ai_detailed_error"] = str(e)[:160]
    else:
        out["ai_detailed"] = "skipped (dry_run)"

    # 3) 신규 파이프라인으로 풀 페이로드 빌드
    g = load_group(parent_asin)
    if not g:
        out["error"] = "load_group None"
        return out
    splits = auto_split(g, "coupang")
    if not splits:
        out["error"] = "auto_split 0"
        return out
    # ★ split 폭발 게이트 — retrofit 도 동일 임계치
    MAX_SPLITS_PER_GROUP = 8
    if len(splits) > MAX_SPLITS_PER_GROUP:
        out["error"] = (f"splits {len(splits)} > {MAX_SPLITS_PER_GROUP} — 그룹화 부적합 "
                        f"(primary={splits[0].get('split_dim')}), retrofit 불가")
        out["n_splits"] = len(splits)
        return out
    # ★ seller_product_id 가 속한 split 매칭 (다중 split 케이스 보호 — 잘못된 split PUT 방지)
    split = None
    if len(splits) > 1:
        from backend.purchase.database import get_db as _gdb_match
        with _gdb_match() as _c:
            _row = _c.execute(
                """SELECT p.asin FROM listings_pa l JOIN products p ON l.product_id=p.id
                   WHERE l.channel='coupang' AND l.channel_product_id=?""",
                (str(seller_product_id),),
            ).fetchone()
        if _row:
            _master_asin = _row["asin"]
            for _i, _sp in enumerate(splits):
                _opt_asins = [o.get("asin") for o in _sp.get("options") or []]
                if _master_asin in _opt_asins:
                    split = _sp
                    out["matched_split_index"] = _i
                    break
        if split is None:
            out["error"] = f"seller_product_id {seller_product_id} 와 splits master 매칭 실패"
            return out
    else:
        split = splits[0]
    pricing = calculate_group_pricing(g, "coupang")
    by_asin = {p["child_asin"]: p for p in pricing}
    opt_asins = [o.get("asin") for o in split.get("options") or []]
    sp_pricing = [by_asin[a] for a in opt_asins if a in by_asin]
    if not sp_pricing:
        out["error"] = "no priced options"
        return out
    # 마진 게이트 — 옵션 중 하나라도 net_margin_krw < 15K 면 retrofit 차단.
    _nets = [p.get("net_margin_krw") or 0 for p in sp_pricing]
    _min_net = min(_nets) if _nets else 0
    if _min_net < 3000:
        out["error"] = f"마진 차단: option min net_margin={_min_net:,}원 < 3,000원"
        return out
    # 4) 기존 master GET — ★2026-08-03: payload 빌드보다 먼저 수행한다.
    #    기존 상품의 displayCategoryCode 를 알아야 신규 옵션의 필수속성을 만들 수 있다.
    #    (기존엔 빌드 후 GET 이라 카테고리를 못 써서 attributes=[] → 전체 거부)
    existing = get_seller_product(str(seller_product_id))
    if not existing or not isinstance(existing.get("data"), dict):
        out["error"] = "get_seller_product 실패/형식 예외"
        return out
    _existing_cat = str((existing.get("data") or {}).get("displayCategoryCode") or "") or None
    if _existing_cat:
        out["inherited_category"] = _existing_cat
        logger.info(f"[group-extend] {parent_asin} 기존 상품 카테고리 {_existing_cat} 상속")

    new_payload = build_coupang_payload(g, split, sp_pricing, requested=requested,
                                        category_override=_existing_cat)
    if not new_payload:
        out["error"] = "build_coupang_payload None (옵션축 매핑 실패/붕괴)"
        return out
    out["rebuilt_items"] = len(new_payload.get("items") or [])
    out["new_seller_product_name"] = new_payload.get("sellerProductName")
    data = existing["data"]
    out["old_seller_product_name"] = data.get("sellerProductName")
    existing_items = list(data.get("items") or [])
    out["old_items"] = len(existing_items)
    # 기존 items 의 asin 역참조 (PA-{pid} 또는 ASIN). 4월 단일경로 sku=PA-pid, 신규경로 sku=asin.
    from backend.purchase.database import get_db as _get_db
    existing_by_asin: dict[str, int] = {}
    with _get_db() as _conn:
        for idx, it in enumerate(existing_items):
            sku = (it.get("externalVendorSku") or "") if isinstance(it, dict) else ""
            asin = None
            if sku.startswith("PA-"):
                try:
                    pid = int(sku[3:])
                    r = _conn.execute("SELECT asin FROM products WHERE id=?", (pid,)).fetchone()
                    if r and r["asin"]:
                        asin = r["asin"]
                except Exception:
                    pass
            elif len(sku) == 10 and sku.isalnum():
                asin = sku
            if asin:
                existing_by_asin[asin] = idx

    # 매칭 → surgical 업데이트(attrs + itemName 만 교체, 나머지 모든 필드 보존).
    # 통째 교체는 "판매중 vendorItem 삭제" 로 쿠팡이 오해함.
    updated = appended = 0
    # 기존 items 의 notices 1개 참조 — 신규 append item 의 notices 가 [] 일 때 복사용.
    # build_coupang_payload 가 meta_cat=None(자동매칭) 케이스에서 notices=[] 로 빌드하므로
    # 그대로 append 하면 쿠팡이 "X번 Options of Notices required" 거절. 같은 sellerProduct
    # 안 기존 item 의 notices 는 카테고리 호환되므로 그대로 복사 가능.
    _reference_notices = None
    for _ei in existing_items:
        if _ei.get("notices"):
            _reference_notices = _ei["notices"]
            break

    # ★2026-08-03: attributes 가 빈 채로 덮이면 쿠팡이 "필수 구매 옵션 존재하지 않습니다" 로
    #   상품 전체를 거부한다(실측 192건 중 144건이 이 경로). 카테고리 미매칭(category="0")이면
    #   build_coupang_payload 가 required_attrs=[] 로 빌드하기 때문.
    #   notices 와 동일하게 "비면 기존 값 보존" 규칙을 적용하고,
    #   신규 append 인데 속성이 비면 그 item 만 건너뛴다(전체 실패 방지).
    skipped_no_attrs = 0
    for new_it in new_payload.get("items") or []:
        new_asin = new_it.get("externalVendorSku")
        _new_attrs = new_it.get("attributes") or []
        if new_asin in existing_by_asin:
            idx = existing_by_asin[new_asin]
            existing_it = existing_items[idx]
            if _new_attrs:
                existing_it["attributes"] = _new_attrs
            elif not existing_it.get("attributes"):
                logger.warning(f"[group-extend] {new_asin} 기존/신규 attributes 모두 없음 — 그대로 둠")
            if new_it.get("itemName"):
                existing_it["itemName"] = new_it["itemName"]
            # 2026-06-04 정책: modelNo는 항상 빈값으로 정리(상품명 등 임의값 금지)
            existing_it["modelNo"] = ""
            updated += 1
        else:
            if not _new_attrs:
                skipped_no_attrs += 1
                logger.warning(f"[group-extend] {new_asin} 필수속성 없음 → append 스킵(전체 거부 방지)")
                continue
            # 신규 append — notices 가 빈 채면 기존 item 의 notices 복사 (auto-match 카테고리 대응)
            if not new_it.get("notices") and _reference_notices:
                new_it["notices"] = _reference_notices
            existing_items.append(new_it)
            appended += 1
    if skipped_no_attrs:
        out["skipped_no_attrs"] = skipped_no_attrs
    out["updated_items"] = updated
    out["appended_items"] = appended
    if updated == 0 and appended == 0:
        out["action"] = "no_change"
        return out
    data["items"] = existing_items
    data["sellerProductName"] = new_payload["sellerProductName"]
    data["displayProductName"] = new_payload["displayProductName"]
    # ★2026-08-03: 쿠팡 상품수정은 부분수정이 아니라 전체 PUT 이라 requested=False 로 보내면
    #   상품 전체가 임시저장으로 내려가 판매가 중단된다(실측: 승인완료 상품이 임시저장중으로 전락).
    #   기존 상품이 판매 단계면 requested=True 를 강제해 판매 상태를 지킨다.
    _cur_status = str((existing.get("data") or {}).get("statusName") or "")
    _live = any(k in _cur_status for k in ("승인완료", "승인대기", "심사"))
    _req_eff = True if _live else bool(requested)
    if _live and not requested:
        logger.info(f"[group-extend] {parent_asin} 기존상태={_cur_status} → requested=True 강제(판매 유지)")
    out["existing_status"] = _cur_status
    out["requested_effective"] = _req_eff
    data["requested"] = _req_eff

    if dry_run:
        out["action"] = "dry_run"
        return out

    # 5) PUT (Coupang update endpoint = no ID in path, sellerProductId in body)
    path = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
    try:
        r = _request_with_retry("PUT", BASE + path,
                                headers=_signature("PUT", path), json=data, timeout=30)
    except Exception as e:
        out["error"] = f"PUT 예외: {e}"
        return out
    if r is None or r.status_code >= 400:
        status = r.status_code if r is not None else "no-resp"
        body = r.text[:400] if r is not None else ""
        out["action"] = "extend_failed"
        out["error"] = f"PUT {status}: {body}"
        return out
    out["action"] = "extended"
    out["put_status"] = r.status_code
    try: out["put_body"] = r.json()
    except Exception: out["put_body"] = r.text[:200]
    return out


def resync_group_from_spapi(parent_asin: str) -> Optional[str]:
    """리스팅 직전 SP-API 로 그룹 관계 재동기화 (정합성 게이트).

    4월 적재 variation_groups 가 현재 SP-API 와 어긋난 경우(잘못된 부모-자식 조립, 존재하지 않는
    유령 부모 ASIN)를 교정한다. 유효 부모 → child_asins 최신화(upsert) + 신규 자식 products 적재.
    실패 시 사유 문자열 반환(caller 가 해당 그룹 리스팅 skip), 성공 시 None.
    """
    from backend.purchase.services.sp_api_group_discovery import discover_group
    meta = discover_group(parent_asin)
    if not meta:
        return "SP-API 부모 조회 실패 (NOT_FOUND/유령 ASIN 가능)"
    if not meta.get("child_asins"):
        return "자식 ASIN 0개 (변형 그룹 아님)"
    try:
        fetch_and_insert_children(parent_asin)
    except Exception as e:
        logger.warning(f"[resync] {parent_asin} 자식 적재 일부 실패: {e}")
    return None


def _single_fallback_split(split, sp_pricing, requested, dry_run) -> dict:
    """멀티옵션 빌드 실패 split(카테고리 색상축 거부·붕괴 등) → 자식을 개별 단일로 등록.
    near-dup dedup(노이즈 Black/Black 3 합침) + list_product 가드(이미등록·중복ASIN)로 중복 방지."""
    import re as _re
    from backend.purchase.services.coupang_lister import list_product as _lp
    def _key(c):
        s = (c or "").strip().lower()
        s = _re.sub(r"[\s,]*\d+\s*$", "", s)          # 끝 숫자(black 3→black)
        s = _re.sub(r"\b(pack|set|pcs?|개입|세트)\b", "", s)  # 흔한 노이즈어
        return _re.sub(r"\s+", " ", s).strip() or (c or "").strip().lower()
    seen = set(); listed = 0; skipped = 0
    for p in sp_pricing:
        k = _key(p.get("color") or p.get("option_label") or "")
        if k and k in seen:
            skipped += 1; continue
        if k: seen.add(k)
        pid = p.get("child_product_id")
        if not pid:
            skipped += 1; continue
        if dry_run:
            listed += 1; continue
        try:
            r = _lp(pid, requested=requested)
            if isinstance(r, dict) and r.get("ok"):
                listed += 1
            else:
                skipped += 1
        except Exception as e:
            skipped += 1
            logger.warning(f"[single-fallback] pid {pid}: {str(e)[:60]}")
    return {"listed": listed, "skipped_dup": skipped, "distinct": len(seen)}


def _decontaminate_pricing(sp_pricing: list, brand: str = "") -> list:
    """오염 제거 — 자식 제목 유사도(Jaccard) 클러스터링 후 주력(최대) 클러스터만 유지.

    Amazon parent_asin 이 서로 다른 상품을 한 variation family 로 묶는 경우(예: '욕실선반'
    + '샤워캐디')를 탐지해 섞인 소수 상품을 제거. 3개 미만이거나 단일 클러스터면 원본 유지.
    """
    if len(sp_pricing) < 3:
        return sp_pricing
    import re as _re
    asins = [p.get("child_asin") for p in sp_pricing]
    valid_asins = [a for a in asins if a]
    if len(valid_asins) < 3:
        return sp_pricing
    titles: dict = {}
    try:
        from backend.purchase.database import get_db as _get_db
        with _get_db() as _c:
            ph = ",".join("?" * len(valid_asins))
            for r in _c.execute(
                f"SELECT asin, title_ko FROM products WHERE asin IN ({ph})", valid_asins
            ).fetchall():
                titles[r["asin"]] = r["title_ko"] or ""
    except Exception as _e:
        logger.warning(f"[group-decontam] title 조회 실패(원본유지): {_e}")
        return sp_pricing
    _STOP = {"세트", "대용량", "수납", "휴대용", "여행용", "블랙", "화이트", "실버", "핑크",
             "베이지", "그레이", "블루", "레드", "그린", "색상", "컬러", "개입", "스타일", "옵션"}
    _bw = (brand or "").strip()
    def _cw(t):
        ws = _re.sub(r"[0-9]+|[A-Za-z]+", " ", t or "").split()
        return set(w for w in ws if len(w) >= 2 and w not in _STOP and w != _bw)
    sets = [_cw(titles.get(a, "")) for a in asins]
    n = len(sp_pricing)
    par = list(range(n))
    def _find(x):
        while par[x] != x:
            par[x] = par[par[x]]; x = par[x]
        return x
    def _jac(a, b):
        return (len(a & b) / len(a | b)) if (a | b) else 0.0
    for i in range(n):
        for j in range(i + 1, n):
            if _jac(sets[i], sets[j]) >= 0.34:
                par[_find(i)] = _find(j)
    clusters: dict = {}
    for i in range(n):
        clusters.setdefault(_find(i), []).append(i)
    if len(clusters) <= 1:
        return sp_pricing
    biggest = max(clusters.values(), key=len)
    # ★주력 클러스터가 과반 미만이면 클러스터링 불신 → 전부 유지 (2026-06-30).
    #   같은 상품의 색/사이즈 변형이 번역 편차로 제각각 단어가 되면(예 철봉: 블랙"중량지지" vs
    #   실버"강철파이프") 안 뭉쳐 1개로 붕괴. 명확한 과반 오염일 때만 제거, 파편화면 보류.
    if len(biggest) * 2 <= n:
        logger.info(f"[group-decontam] {brand} 클러스터 파편화(주력 {len(biggest)}/{n}, 과반미만) → 전부 유지(오염판정 보류)")
        return sp_pricing
    logger.info(f"[group-decontam] {brand} 오염 {len(clusters)}클러스터 → 주력 {len(biggest)}/{n} 유지")
    return [sp_pricing[i] for i in sorted(biggest)]


# ★쿠팡 옵션 한도 — 정제 후 옵션수가 이 값을 넘으면 그룹 대신 자식 단품 폴백.
#   2026-08-03: 공식문서 재확인 결과 실제 한도는 200 (기존 100 은 과소값이라
#   101~200 개 그룹이 불필요하게 단품 폴백됐다). variation.CHANNEL_LIMIT 과 일치시킨다.
MAX_GROUP_OPTIONS = 30


def register_new_group_listing(
    parent_asin: str,
    channels: list[str] | None = None,
    dry_run: bool = True,
    split_indices: list[int] | None = None,
    *,
    requested: bool = True,
    skip_archive: bool = False,
    fast_detail: bool = False,
    max_options: int | None = None,
) -> dict:
    """B 모드: 옵션 그룹을 처음부터 multi-option 으로 신규 등록.

    skip_archive: True 면 같은 group 기존 단일 listing archive(삭제) 생략 — OLD→NEW
      마이그레이션처럼 다른 계정으로 옮길 때 OLD 단일을 지우면 안 되는 경우.

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


    # ── 삭제 이력 검사 (parent_asin) — 재등록 방지 ──
    from backend.purchase.services.delete_history import is_previously_deleted
    _blocked_del, _reason_del = is_previously_deleted(parent_asin)
    if _blocked_del:
        return {"parent_asin": parent_asin, "mode": "skip",
                "error": f"previously_deleted:{_reason_del}"}

    channels = channels or ["smartstore", "coupang"]
    # ★ 그룹 경로 차단 게이트 (단일 경로 모든 정책 미러 — 계정 품질·정지 예방)
    _block = _group_path_blocking_gates(parent_asin, channel="coupang")
    if _block:
        return {"error": f"blocked: {_block}", "parent_asin": parent_asin}
    # ★ 리스팅 직전 SP-API 재동기화 — 4월 적재 데이터 정합성 교정 + 유령 부모 차단
    _resync_err = resync_group_from_spapi(parent_asin)
    if _resync_err:
        return {"error": f"group resync: {_resync_err}", "parent_asin": parent_asin}

    # ★ cost_usd backfill — BuyBox/Lowest 가격 책정 (NULL child 만 SP-API Pricing 호출).
    # 누락된 1~2개 옵션을 살리는 정도이므로 쿠팡 SellerProduct 등록량 vs 회수율 균형.
    # NULL 없으면 즉시 반환 — 매번 호출해도 비용 0. dry_run 무관 (DB 사전 정합성용).
    cost_backfill = None
    import os as _osc
    if _osc.environ.get("PA_SKIP_GEMINI") == "1":
        # ★고속모드: SP-API cost-backfill 스킵(0.5RPS pricing 스로틀 병목). 등록 자식은 pre-price됨.
        cost_backfill = "skipped(fast)"
    else:
        try:
            cost_backfill = assign_cost_via_pricing(parent_asin)
        except Exception as e:
            logger.warning(f"[register-new-group] {parent_asin} cost_backfill 부분 실패(계속): {e}")
            cost_backfill = f"error: {str(e)[:160]}"

    # ★ AI detailing — title_ko/desc_ko 채움 (sellerProductName 한글화 위해).
    # dry_run 에선 비용 큰 batch 스킵(빌드 검증 목적). 라이브에선 실행.
    ai_detailed = None
    if not dry_run:
        try:
            from backend.purchase.services.keyword_to_groups import _run_ai_detailing_for_group
            ai_detailed = _run_ai_detailing_for_group(parent_asin, platform="coupang")
        except Exception as e:
            logger.warning(f"[register-new-group] {parent_asin} AI detailing 부분 실패(계속): {e}")
            ai_detailed = f"error: {str(e)[:160]}"

    group = load_group(parent_asin)
    if not group:
        return {"error": f"group {parent_asin} 없음"}

    out = {"parent_asin": parent_asin, "mode": "register", "dry_run": dry_run,
           "cost_backfill": cost_backfill, "ai_detailed": ai_detailed, "channels": {}}
    analysis = analyze_group_listings(parent_asin)
    out["analysis"] = analysis

    MAX_SPLITS_PER_GROUP = 8  # 부모 1개당 쿠팡 sellerProduct 등록 상한
    for ch in channels:
        splits = auto_split(group, ch)
        # ★ split 폭발 게이트 — primary cardinality 너무 큰 그룹은 단일 경로로 fallback
        if splits and len(splits) > MAX_SPLITS_PER_GROUP:
            out["channels"][ch] = [{
                "status": "skipped_high_cardinality",
                "n_splits": len(splits),
                "max_allowed": MAX_SPLITS_PER_GROUP,
                "primary_dim": splits[0].get("split_dim"),
                "reason": f"splits {len(splits)} > {MAX_SPLITS_PER_GROUP} — 그룹화 부적합, 단일 경로 권장",
            }]
            continue
        pricing = calculate_group_pricing(group, ch)
        by_asin = {p["child_asin"]: p for p in pricing}

        ch_results = []
        # ★auto_split=0(서술형 사이즈 등으로 그룹화불가) → 소실 대신 자식 단품복구 (2026-07-08).
        #   dedup은 option_label(사이즈 포함) 기준 — color만으론 with/without Bottom 같은 사이즈변형 오병합.
        if not splits and ch == "coupang":
            _all = _decontaminate_pricing(list(pricing), group.get("brand") or "")
            from backend.purchase.services.coupang_lister import list_product as _lp_r
            _seen = set(); _listed = _skip = 0
            for _p in _all:
                _k = (_p.get("option_label") or _p.get("child_asin") or "").strip().lower()
                if not _k or _k in _seen:
                    _skip += 1; continue
                _seen.add(_k)
                _pid = _p.get("child_product_id")
                if not _pid:
                    _skip += 1; continue
                if dry_run:
                    _listed += 1; continue
                try:
                    _r = _lp_r(_pid, requested=requested)
                    if isinstance(_r, dict) and _r.get("ok"):
                        _listed += 1
                    else:
                        _skip += 1
                except Exception as _e:
                    _skip += 1
                    logger.warning(f"[autosplit0-recover] pid {_pid}: {str(_e)[:60]}")
            ch_results.append({"split_index": 0, "status": "single_fallback",
                               "reason": "auto_split=0(그룹화불가)→단품복구",
                               "listed": _listed, "skipped_dup": _skip, "distinct": len(_seen)})
            out["channels"][ch] = ch_results
            continue
        for sp_idx, split in enumerate(splits):
            if split_indices is not None and sp_idx not in split_indices:
                continue
            opt_asins = [o.get("asin") for o in split.get("options") or []]
            sp_pricing = [by_asin[a] for a in opt_asins if a in by_asin]
            # ★오염 제거 — Amazon variation에 섞인 다른상품 컷(자식 제목 클러스터링, 주력만 유지)
            sp_pricing = _decontaminate_pricing(sp_pricing, group.get("brand") or "")
            if not sp_pricing:
                ch_results.append({"split_index": sp_idx, "status": "skipped", "reason": "no pricing"})
                continue

            # ★옵션 한도 초과 → 단품 폴백 (억지 그룹화/등록거부 방지). 쿠팡 한정.
            if ch == "coupang" and len(sp_pricing) > MAX_GROUP_OPTIONS:
                logger.info(f"[group-cap] {group.get('parent_asin')} 옵션 {len(sp_pricing)} > {MAX_GROUP_OPTIONS} → 단품 폴백")
                _sf = _single_fallback_split(split, sp_pricing, requested, dry_run)
                ch_results.append({"split_index": sp_idx, "status": "single_fallback",
                                   "reason": f"옵션 {len(sp_pricing)} > {MAX_GROUP_OPTIONS} 한도초과", **_sf})
                continue

            if ch == "smartstore":
                payload = build_smartstore_payload(group, split, sp_pricing)
            else:
                # 마진 게이트 (쿠팡만) — 옵션 중 하나라도 net_margin_krw < 15K 면 split 전체 차단.
                _nets = [p.get("net_margin_krw") or 0 for p in sp_pricing]
                _min_net = min(_nets) if _nets else 0
                if _min_net < 3000:
                    ch_results.append({
                        "split_index": sp_idx, "status": "skipped",
                        "reason": f"마진 차단: option min net_margin={_min_net:,}원 < 3,000원",
                    })
                    continue
                # ★ 리치 상세 자동생성(B 통합) — fast_detail(디테일링 분리)이면 생략(진짜 빠른 임시저장).
                #   생략 시 seo_detail.json 미생성 → build_detail_contents가 fast 경로(실제컷)로 빌드.
                if not dry_run and not fast_detail:
                    try:
                        from backend.purchase.services.seo_detail_gen import generate_seo_detail
                        for _sp in sp_pricing:
                            if _sp.get("child_product_id"):
                                generate_seo_detail(_sp["child_product_id"])
                    except Exception as _e:
                        logger.warning(f"[register-new-group] seo_detail 생성 실패(인포 폴백): {_e}")
                payload = build_coupang_payload(group, split, sp_pricing, requested=requested,
                                                fast_detail=fast_detail, max_options=max_options)
            if not payload:
                # ★ 멀티옵션 빌드 실패(카테고리 색상축 거부·붕괴 등) → 단일폴백: 자식 개별 단일(dedup+가드로 중복방지).
                _sf = _single_fallback_split(split, sp_pricing, requested, dry_run)
                ch_results.append({"split_index": sp_idx, "status": "single_fallback", **_sf})
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
                    # ★brandId 등록이 게이팅 등으로 실패하면 노브랜드 재빌드 1회 폴백
                    if (not res or not res.get("data")) and payload.get("brandId"):
                        logger.warning(f"[group-lister] {master_asin} brandId={payload.get('brandId')} 등록실패 → 노브랜드 폴백")
                        _pnb = build_coupang_payload(group, split, sp_pricing, requested=requested,
                                                     force_no_brand=True, fast_detail=fast_detail, max_options=max_options)
                        if _pnb:
                            payload = _pnb
                            res = cp_register(payload)
                    if not res or not res.get("data"):
                        # ★멀티옵션 등록 거부(옵션 속성값 거부 등, 예: 침구류사이즈 free-text 치수 거부) → 단품 폴백.
                        #   단품 경로는 옵션 속성 검증을 안 거쳐 회피됨. (2026-06-30)
                        logger.info(f"[group-lister] {master_asin} 멀티옵션 등록거부 → 단품 폴백: {str(res)[:120]}")
                        _sf = _single_fallback_split(split, sp_pricing, requested, dry_run)
                        ch_results.append({"split_index": sp_idx, "status": "single_fallback",
                                           "reason": "멀티옵션 등록거부", **_sf})
                        continue
                    seller_product_id = str(res["data"])
                    # 승인 요청은 페이로드 requested=True 로 자동 트리거됨 (별도 PUT 불필요)
                    option_id_map = _extract_coupang_option_ids(seller_product_id)
                    channel_product_id = seller_product_id
                    smartstore_channel_no = None

                # listings_pa + listing_options 매핑 INSERT
                base_pricing = master_pricing
                # ★ 관세필터 미러 — build_coupang_payload가 원가>$150 옵션을 payload에서 제외하므로
                #   listing_options 도 동일 제외(쿠팡 실등록과 DB 일치). [[customs gate]]
                from backend.purchase.services.coupang_lister import _exceeds_customs_limit as _excl
                options_for_persist = []
                for p in sp_pricing:
                    if ch == "coupang" and _excl(p.get("cost_usd")):
                        continue
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
        # ★ 2026-06-20: dry_run 이거나 skip_archive(마이그레이션) 면 archive 생략.
        #   기존 버그: archive 가 dry_run 가드 밖이라 dry_run 에서도 DELETE 실행(같은계정이면 실삭제 위험).
        #   마이그레이션(OLD→NEW): OLD 단일을 지우면 안 됨.
        # archive 는 라이브 + 같은계정(skip_archive=False)일 때만. dry_run/마이그레이션은 생략.
        if not (dry_run or skip_archive):
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
                              split_indices: list[int] | None = None,
                              *, requested: bool = True, skip_archive: bool = False,
                              fast_detail: bool = False) -> dict:
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
        # ★2026-08-01: requested/skip_archive/fast_detail 전달 누락 버그 수정.
        #   기존엔 기본값(requested=True, skip_archive=False)으로 떨어져
        #   임시저장 정책이 깨지고 기존 단일 리스팅이 archive(삭제)됐다.
        result = register_new_group_listing(parent_asin, channels, dry_run=dry_run,
                                             split_indices=split_indices,
                                             requested=requested, skip_archive=skip_archive,
                                             fast_detail=fast_detail)
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

        _why: list = []
        if ch == "smartstore":
            payload = build_smartstore_payload(group, master_split, sp_pricing, reasons=_why)
        else:
            payload = build_coupang_payload(group, master_split, sp_pricing, reasons=_why)

        if not payload:
            out["channels"][ch] = {"action": "error",
                                   "reason": "; ".join(_why) or "페이로드 빌드 실패 (사유 미기록)"}
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
                group_names=oi.get("optionCombinationGroupNames") or None,
            )
            if not res:
                out["channels"][ch] = {"action": "extend_failed", "stage": "naver_extend"}
                continue

            # ★성공 응답을 믿지 않고 저장 결과를 되읽는다(2026-04-24 사고 재발 방지).
            #   단독형으로 저장되면 자식 ASIN 매핑 자리가 없어 주문이 오는 순간 오배송이다.
            #   여기서 걸러 두지 않으면 '정상 등록'으로 기록돼 몇 달 뒤에나 드러난다.
            struct_ok, struct_why = verify_naver_option_structure(
                origin_no, len(oi.get("optionCombinations") or []))
            if not struct_ok:
                logger.error("[naver-옵션구조] listing %s (상품 %s): %s",
                             master["listing_id"], origin_no, struct_why)
                # 자식은 그대로 두고(통합 전 상태 보존) 마스터에 사유를 남긴 뒤 멈춘다.
                with get_db() as conn:
                    conn.execute(
                        "UPDATE listings_pa SET error_message=? WHERE id=?",
                        (f"[옵션구조 검증실패] {struct_why}", master["listing_id"]),
                    )
                out["channels"][ch] = {
                    "action": "extend_unverified",
                    "extend_ok": True,
                    "structure_ok": False,
                    "reason": struct_why,
                }
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
                "structure_ok": True,
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

        # ★2026-08-02: extend 로 추가한 옵션을 listing_options 에 기록.
        #   기존엔 has_options=1 만 켜고 옵션 행을 안 만들어, 쿠팡엔 등록됐는데 DB 는 모르는
        #   상태가 됐다(다음 실행 때 중복 추가 시도 + 무추적 재발).
        _opt_added = 0
        try:
            _id_map = _extract_coupang_option_ids(str(seller_id)) or {}
            _by_asin = {p.get("child_asin"): p for p in (sp_pricing or [])}
            _ts_now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with get_db() as conn:
                for _it in new_items:
                    _sku = _it.get("externalVendorSku")
                    if not _sku:
                        continue
                    _row = conn.execute(
                        "SELECT id FROM products WHERE asin=?", (_sku,)
                    ).fetchone()
                    if not _row:
                        continue
                    _pr = _by_asin.get(_sku) or {}
                    conn.execute(
                        """INSERT INTO listing_options
                             (listing_id, child_product_id, option_label, channel_option_id,
                              sale_krw, cost_krw_snapshot, net_margin_krw, stock,
                              status, last_synced_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                           ON CONFLICT(listing_id, child_product_id) DO UPDATE SET
                             channel_option_id=COALESCE(excluded.channel_option_id, listing_options.channel_option_id),
                             option_label=excluded.option_label,
                             last_synced_at=excluded.last_synced_at""",
                        (master["listing_id"], _row["id"],
                         _it.get("itemName") or _pr.get("option_label") or "",
                         _id_map.get(_sku), _pr.get("sale_krw"), _pr.get("cost_krw"),
                         _pr.get("net_margin_krw"), _pr.get("stock") or 100, _ts_now),
                    )
                    _opt_added += 1
            logger.info(f"[coupang-extend] {seller_id} listing_options {_opt_added}건 기록 "
                        f"(vendorItemId 매핑 {sum(1 for k in _id_map if k in {i.get('externalVendorSku') for i in new_items})}건)")
        except Exception as _e_opt:
            logger.error(f"[coupang-extend] {seller_id} listing_options 기록 실패: {_e_opt}")

        out["channels"][ch] = {
            "action": "extended",
            "extend_ok": True,
            "needs_reapproval": True,
            "options_recorded": _opt_added,
            "subordinates_stopped": sum(1 for s in sub_results if s["ok"]),
            "subordinates_total": len(sub_listings),
        }

    return out


# ── 옵션 차원 추출 헬퍼 ───────────────────────────────
def _extract_option_values(split: dict) -> list[dict]:
    """split.options 의 children 으로부터 차원별 unique 값 목록 추출.

    반환: [{"groupName": "사이즈", "values": ["14온스","20온스",...]}, ...]
    """
    from backend.purchase.services.variation import korean_label, _strip_common_affix, _is_color_like

    options = split.get("options") or []
    # 지원 차원: 4 core (size/color/flavor/style) 만. material/model_number 는 SP-API
    # variationTheme 이 명시한 axis 가 아닐 가능성 높아 옵션 라벨 오염 (예: model_number
    # 슬롯에 사이즈 "SM 14"·"2set adults" 가 들어가 5축으로 분리). 수량(number_of_items
    # /package_quantity) 도 제외 + 모든 옵션에 "1개" 강제(아래 mandatory fill).
    SUPPORTED_DIMS = ("size_label", "color", "flavor_attr", "style")
    DIM_GROUP_NAME = {
        "size_label": "사이즈", "color": "색상", "flavor_attr": "맛", "style": "스타일",
    }
    # 차원이 변형으로 인정되려면 ≥2 distinct 값 필요 (단일 값이면 product-common, 옵션축 아님)
    dim_keys = []
    for k in SUPPORTED_DIMS:
        vals = {c.get(k) for c in options if c.get(k) is not None and c.get(k) != ""}
        if len(vals) >= 2:
            dim_keys.append(k)
    color_vals = {c.get("color") for c in options if c.get("color")}

    def _to_display_str(k: str, v) -> str:
        """raw 값 → 표시용 str. 숫자 차원은 단위 부여 (10 → '10개')."""
        if k in ("number_of_items", "package_quantity") and not isinstance(v, str):
            try: return f"{int(v)}개"
            except Exception: return str(v)
        return v if isinstance(v, str) else str(v)

    out = []
    for k in dim_keys:
        # raw 값 (child.get() 그대로 — int 일 수 있음) 와 display 값 분리.
        # value_map keys = raw 값 → Loop A 의 child.get(k) 로 직접 lookup 가능.
        raw_seen, display_seen = [], []
        for c in options:
            v = c.get(k)
            if v is None or v == "":
                continue
            if v in raw_seen:
                continue
            raw_seen.append(v)
            display_seen.append(_to_display_str(k, v))
        if not raw_seen:
            continue
        # size 차원인데 값이 전부 색상값(color 차원과 중복 or 색상사전 매칭) → 색상 오염, 스킵
        if k == "size_label" and all((v in color_vals) or _is_color_like(v) for v in display_seen):
            continue
        # 형제 공통 접미/접두 토큰 제거(예: 모두 '-accessory') 후 한글화 → value_map(raw→정규화)
        cleaned = _strip_common_affix(display_seen)
        kor_values = [korean_label(cv) or cv for cv in cleaned]
        # ★옵션 라벨 오염 방지 (2026-06-30) — 한글화 후에도 서술형/정크 축은 옵션축에서 제외.
        #   "2.2L+1.2L+1L pot+19.1cm"(+결합·단위·60자초과 → is_noisy) → 제외.
        #   style/flavor 슬롯의 순수 숫자("8") 정크 → 제외. 깨끗한 축 0개면 호출부 가드가 None→단품폴백.
        from backend.purchase.services.variation import is_noisy_axis_value as _noisy_axis
        # ★2026-08-04: 기존 any() 는 값 하나만 노이즈여도 축 전체를 폐기했다.
        #   ('Carbon Fiber','Grey Stitches','Red+Black' 중 마지막 하나 때문에 색상축 전멸
        #    → dim_groups 빈값 → 페이로드 None → 그룹 통째로 단품폴백)
        #   소수(≤1/3)면 축을 살린다. 과반 노이즈면 기존대로 폐기(서술형 축 오염 방지 유지).
        _noisy_n = sum(1 for v in kor_values if _noisy_axis(v))
        if _noisy_n and (_noisy_n * 3 > len(kor_values) or (len(kor_values) - _noisy_n) < 2):
            continue
        # ★2026-08-04: 서술형 축 차단. 개별 값이 정규식 노이즈에 안 걸려도, 값이 길면
        #   그건 옵션값이 아니라 서술(차량 호환표기 등)이다.
        #   예: 색상 '2Pcs for 2020 2025 Silverado Sierra'(35자) → 옵션축 아님.
        #   진짜 옵션값은 짧다('Carbon Fiber' 12, 'Red+Black' 9).
        #   ★색상축에만 적용(2026-08-04 보정): 전 축 적용은 과차단이었다
        #   (사이즈/스타일 라벨은 정당하게 길 수 있음 → 빈값 29→47 악화).
        #   색상명은 짧다. 길면 색상이 아니라 서술이다.
        if k == "color":
            _AXIS_VALUE_MAXLEN = 25
            _long_n = sum(1 for v in kor_values if len(str(v)) > _AXIS_VALUE_MAXLEN)
            if _long_n * 3 > len(kor_values):
                continue
        if k in ("style", "flavor_attr") and any(str(v).strip().isdigit() for v in kor_values):
            continue
        value_map = {raw: kv for raw, kv in zip(raw_seen, kor_values)}
        out.append({"key": k, "groupName": DIM_GROUP_NAME[k], "values": kor_values,
                    "raw_values": raw_seen, "value_map": value_map})
    return out


def _option_label_for_child(child: dict, dim_groups: list[dict]) -> str:
    from backend.purchase.services.variation import korean_label
    parts = []
    for dg in dim_groups:
        v = child.get(dg["key"])
        if v:
            parts.append(dg.get("value_map", {}).get(v) or korean_label(v) or v)
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
        # ★정렬 없는 LIMIT 1 은 '가장 오래된' 행을 집는다. 채널마다 pid 를 만드는
        #   신 파이프라인에서는 그게 식별자·상세가 빈 행일 수 있다(실측: EAN 보유 상품이
        #   barcode='' 로 등록됐다). 식별자 있는 행 → 최신 행 순으로 고른다.
        p = conn.execute(
            "SELECT * FROM products WHERE asin=?"
            " ORDER BY (identifiers_json IS NOT NULL AND identifiers_json <> '') DESC,"
            "          id DESC LIMIT 1", (master_asin,)
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
def _pl_fail(reasons, msg: str):
    """페이로드 빌드 실패 사유 기록 (2026-08-01).
    기존엔 None 만 반환해 호출측이 '이미지/master 미보유 가능' 이라는 뭉뚱그린
    메시지밖에 못 남겼다. 실제 사유(옵션축 붕괴·관세초과 등)를 전달한다."""
    if reasons is not None:
        reasons.append(msg)
    return None


def _ss_bundle_group() -> dict:
    from backend.purchase.services.smartstore_lister import _bundle_group
    return _bundle_group()


def _ss_shipping_id() -> int:
    from backend.purchase.services.smartstore_lister import _shipping_address_id
    return _shipping_address_id()


def _ss_return_id() -> int:
    from backend.purchase.services.smartstore_lister import _return_address_id
    return _return_address_id()


def build_smartstore_payload(
    group: dict, split: dict, pricing_for_split: list[dict],
    reasons: list | None = None,
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
        return _pl_fail(reasons, "[group-lister] split '…' — products 테이블에 children 0건")
    if not product:
        logger.warning(f"[group-lister-smartstore] master {master.get('asin')} products 없음")
        return _pl_fail(reasons, "[group-lister-smartstore] master … products 없음")

    # 한국 마켓 표시용 → title_ko 우선(AI 번역 결과). 없으면 split.name(보통 영문 그룹명), 최후 title_en.
    name = (product.get("title_ko") or split.get("name") or product.get("title_en") or "").strip()
    if len(name) > 80:
        name = name[:80].rstrip()   # 네이버 100자 hard, 80자 권장 (검색 노출)
    # 2026-08-03: 구매대행 [해외] 표기가 smartstore_lister 에만 있고 이쪽엔 없어
    #   그룹 등록분 53% 가 태그 없이 올라갔다(실측 77/145). 동일 정제·태그를 적용한다.
    from backend.purchase.services.smartstore_lister import _clean_product_name as _cpn
    from backend.purchase.services import clean_policy as _cp
    name = _cp.ensure_overseas_tag(_cpn(name), max_len=50)
    category = product.get("category_path") or ""
    if not name or not category:
        return None

    # pricing 매핑 (asin → sale_krw)
    by_asin = {p["child_asin"]: p for p in pricing_for_split}
    valid = [(o, by_asin.get(o.get("asin"))) for o in options if by_asin.get(o.get("asin"))]
    # ★2026-08-03 네이버 카테고리 게이트 — 옵션 단위로 거른다.
    #   부모를 통째로 올리면 배정 안 된 형제까지 옵션으로 딸려온다(실측 144건 유입).
    #   단품 경로(smartstore_lister)만 막아서는 이 경로로 새므로 여기서도 차단한다.
    try:
        from backend.purchase.services import clean_policy as _cp_gate
        from backend.purchase.services.naver_commerce_service import active_account as _na_gate
        from backend.purchase.database import get_db as _gdb_gate
        _acct_gate = _na_gate()
        _kept, _blocked = [], 0
        with _gdb_gate() as _gc:
            for _o, _pr in valid:
                _pid = (_pr or {}).get("child_product_id")
                _row = _gc.execute(
                    "SELECT amazon_category_json, sp_product_type, "
                    "sp_browse_classification, sp_website_display_group, title_ko, title_en "
                    "FROM products WHERE id=?", (_pid,)
                ).fetchone() if _pid else None
                _ok, _why, _cls = _cp_gate.check_naver_gate_by_product(_row, _acct_gate)
                if _ok:
                    _kept.append((_o, _pr))
                else:
                    _blocked += 1
        if _blocked:
            logger.info(f"[naver-gate] {group.get('parent_asin')} 옵션 {_blocked}개 차단 "
                        f"(잔여 {len(_kept)}) — 화장품/식품 외 제외")
        valid = _kept
    except Exception as _ge:
        logger.warning(f"[naver-gate] 그룹 옵션 검사 실패(통과): {_ge}")
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
            # ★조합형 필수 — 없으면 네이버가 optionCombinations 를 무시한다(2026-08-08 실증)
            "optionCombinationGroupNames": {
                f"optionGroupName{i + 1}": dg["groupName"]
                for i, dg in enumerate(dim_groups[:4])
            },
            "optionCombinations": option_combinations,
            "useStockManagement": True,
        },
    }

    # 2026-08-03: 가격표시제 대상(샴푸/세제 등) 필수필드. 대상이 아니면 무시되므로 항상 넣는다.
    detail_attribute.setdefault("unitCapacity", {"unitPriceYn": False})
    detail_attribute.setdefault("unitQuantity", {"unitPriceYn": False})

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
                # 2026-08-03: 묶음배송그룹/출고지/반품지가 전부 구계정 값으로 하드코딩돼 있었다
                #   (57248768 / 200297709 / 200335116). 신계정에 그대로 보내면 400.
                #   smartstore_lister 는 계정별 함수를 쓰는데 이쪽만 누락돼 그룹 경로가 전건 실패했다.
                **_ss_bundle_group(),
                "deliveryFee": {"deliveryFeeType": "FREE"},
                "claimDeliveryInfo": {
                    "returnDeliveryCompanyPriorityType": "PRIMARY",
                    "returnDeliveryFee": 5000,
                    "exchangeDeliveryFee": 5000,
                    "shippingAddressId": _ss_shipping_id(),
                    "returnAddressId": _ss_return_id(),
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


def _resolve_group_category(product: dict, title_hint: str = "") -> Optional[str]:
    """그룹 master 카테고리 해석 — listings_pa 미보유 신규 패밀리용 (2026-06-03).

    그룹 빌드는 listings_pa.coupang_category_code 에서만 카테고리를 읽어, 처음 등록되는
    패밀리는 cat=0 → 차원 화이트리스트 없음. 단일 경로와 동일하게 키워드-카테고리 캐시로 해석.
    ★ 2026-06-03 수정: RAG(resolve_category)는 naver_categories 기반 = 네이버 카테고리 id 반환.
    이걸 쿠팡 코드로 쓰면 get_category_meta 가 400 ("관리카테고리") → 무효 + 낭비 호출.
    → RAG 폴백 제거. 키워드캐시(coupang_category_code)만 신뢰. 미스 시 None → cat=0 → 자식단일 폴백
    (단일 경로도 키워드 없으면 cat=0 auto-match 이므로 일관).
    반환: coupang_category_code(str) or None.
    """
    pid = product.get("id")
    # ★신 파이프라인 M12 가 확정한 쿠팡 카테고리를 최우선으로 쓴다 (2026-08-13).
    #   import_category 가 없으면(구 파이프라인) 아래 종전 경로로 그대로 내려간다 — 회귀 0.
    #   이게 없으면 신규 패밀리가 매번 "확신부족 → 단일유지" 로 그룹이 깨진다.
    try:
        from backend.purchase.database import get_db as _get_db
        _pa = product.get("parent_asin")
        with _get_db() as _c:
            if not _pa and pid:
                _r = _c.execute("SELECT parent_asin FROM products WHERE id=?", (pid,)).fetchone()
                _pa = _r["parent_asin"] if _r else None
            if _pa:
                _cr = _c.execute(
                    "SELECT cat_code FROM import_category"
                    " WHERE parent_asin=? AND channel='coupang' AND cat_code IS NOT NULL"
                    " AND cat_code<>'' ORDER BY rowid DESC LIMIT 1", (_pa,)).fetchone()
                if _cr and _cr["cat_code"]:
                    logger.info("[group-cat] import_category %s → %s (M12 확정)"
                                % (_pa, _cr["cat_code"]))
                    return str(_cr["cat_code"])
    except Exception as _e:  # noqa: BLE001
        logger.warning("[group-cat] import_category 조회 실패(계속): %s" % str(_e)[:80])

    try:
        from backend.purchase.services.channel_listing_service import _resolve_categories_via_cache
        _naver, coup = _resolve_categories_via_cache(pid) if pid else (None, None)
        if coup:
            logger.info(f"[group-cat] product {pid} 키워드캐시 → {coup}")
            return str(coup)
    except Exception as e:
        logger.warning(f"[group-cat] product {pid} 캐시해석 실패: {e}")
    # ★ 2026-06-03: 캐시 미스 → 같은 패밀리의 listed 쿠팡 단일에서 자동할당 카테고리 readback.
    # 재그룹(이미 올라간 단일 묶기)의 정확한 출처 — 쿠팡 ML이 제목/이미지로 할당한 실제값.
    # 신규 드레인 패밀리는 형제 단일이 아직 없어 무동작(다음 라이브폴백으로). API콜은 형제 있을 때만.
    try:
        parent = product.get("parent_asin")
        if parent:
            from backend.purchase.database import get_db as _gdb
            with _gdb() as conn:
                sib = conn.execute(
                    "SELECT l.channel_product_id cpid FROM listings_pa l JOIN products p ON p.id=l.product_id "
                    "WHERE p.parent_asin=? AND l.channel='coupang' AND l.status='listed' "
                    "AND l.channel_product_id IS NOT NULL LIMIT 1", (parent,),
                ).fetchone()
            if sib and sib["cpid"]:
                from backend.purchase.services.coupang_service import get_seller_product
                d = (get_seller_product(str(sib["cpid"])) or {}).get("data") or {}
                cc = d.get("displayCategoryCode")
                if cc and str(cc) != "0":
                    logger.info(f"[group-cat] product {pid} 형제단일 쿠팡 readback → {cc}")
                    return str(cc)
    except Exception as e:
        logger.warning(f"[group-cat] product {pid} readback 실패: {e}")
    # ★ 2026-06-03: 캐시·readback 미스 → 라이브 AI 매핑 폴백 (단일 경로 수준 강화).
    # 기존엔 캐시 미스 = None = cat=0 → 옵션 전부 노이즈 drop → 단일 폴백 누수(~32%).
    # map_categories_for_keyword 가 keyword 없으면 product_name(title)으로 live 매핑 +
    # keyword_category_map 에 캐시 저장(다음 패밀리 hit). 신규+재그룹 양쪽 공유.
    try:
        from backend.purchase.database import get_db
        from backend.purchase.services.category_mapper import map_categories_for_keyword
        kw = None
        if pid:
            with get_db() as conn:
                _kr = conn.execute(
                    "SELECT keyword FROM product_keywords WHERE product_id=? "
                    "ORDER BY is_primary DESC, id ASC LIMIT 1", (pid,),
                ).fetchone()
            kw = _kr["keyword"] if _kr else None
        import os as _os9
        if _os9.environ.get("PA_SKIP_GEMINI") == "1":
            # ★고속모드: Gemini 카테고리(naver+coupang) 스킵 → 단일폴백/ML예측(_predict_category, 비-Gemini)이 처리.
            coup = None
            coup_score = 0
        else:
            res = map_categories_for_keyword(
                keyword=kw,
                product_name=product.get("title_ko") or product.get("title_en") or title_hint or "",
                product_id=pid,
                product_name_en=product.get("title_en") or None,
            )
            coup = res.get("coupang_code") if isinstance(res, dict) else None
            coup_score = int(res.get("coupang_score") or 0) if isinstance(res, dict) else 0
        # ★ 확신도 게이트: 쿠팡 단독 등록이므로 쿠팡 score만 본다(네이버 score 무관 —
        #   결합 needs_review는 naver 실패에 끌려가 그룹경로를 잘못 막음). coup_score>=50(=쿠팡 확실).
        if coup and coup_score >= 50:
            logger.info(f"[group-cat] product {pid} 라이브매핑 -> {coup} (kw={kw!r}, coup_score={coup_score})")
            return str(coup)
        logger.warning(
            f"[group-cat] product {pid} 라이브매핑 확신부족 → 단일유지 "
            f"(coup={coup}, coup_score={coup_score})"
        )
    except Exception as e:
        logger.warning(f"[group-cat] product {pid} 라이브매핑 실패: {e}")
    # ★신규계정: cat=0이면 쿠팡 자동분류 안 함(등록거부 "필수 구매옵션 없음") → 단일경로와 동일 ML예측 폴백(memory ③).
    try:
        from backend.purchase.services.coupang_service import active_account as _aa; _CACT = _aa()
        if _CACT == "new":
            from backend.purchase.services.coupang_lister import _predict_category
            _nm = product.get("title_ko") or product.get("title_en") or title_hint or ""
            _pc, _pn = _predict_category(_nm, product.get("brand") or "")
            if _pc and str(_pc) != "0":
                logger.info(f"[group-cat] product {pid} 신규계정 predict 폴백 → {_pc}({_pn})")
                return str(_pc)
    except Exception as e:
        logger.warning(f"[group-cat] product {pid} predict 폴백 실패: {e}")
    # ★2026-08-02: 이 함수엔 reasons 인자가 없다. 앞선 일괄치환이 잘못 건드려
    #   NameError 로 그룹등록이 전부 실패했다(실측 8/10). 원복.
    return None


# ── 쿠팡 페이로드 ─────────────────────────────────────
import re as _re_qty
_QTY_AXIS_TOKENS = ("수량", "개수", "개당 수량", "패키지수량", "패키지수", "구성수량")
_QTY_VALUE_RE = _re_qty.compile(r"(\d+)\s*(pack|packs|개입|개|팩|piece|pieces|pcs|pc|ea|count|ct|sets|set|세트)", _re_qty.I)


_QTY_OF_RE = _re_qty.compile(r"(?:pack|packs|box|boxes|set|sets|case|cases|bundle|bundles)\s*of\s*(\d+)", _re_qty.I)


def _quantity_axis_name(cat_mand_names):
    for nm in cat_mand_names or []:
        if any(tok in nm for tok in _QTY_AXIS_TOKENS):
            return nm
    return None


def _quantity_value(raw):
    if not raw:
        return None
    s2 = str(raw)
    n = None
    m = _QTY_VALUE_RE.search(s2)
    if m:
        try:
            n = int(m.group(1))
        except (TypeError, ValueError):
            n = None
    if n is None:
        m2 = _QTY_OF_RE.search(s2)
        if m2:
            try:
                n = int(m2.group(1))
            except (TypeError, ValueError):
                n = None
    return (str(n) + "개") if (n and n > 0) else None


def _strip_group_option_words(name: str, options: list) -> str:
    """그룹 상품명에서 옵션값(색상/사이즈/맛/스타일)을 제거 — 쿠팡이 옵션을 상품명 뒤에 자동 노출하므로
    상품명에 옵션단어가 있으면 '옵션과 중복' 경고+수정요청 발생(2026-07-01). 단어경계로만 제거(블랙박스 보호)."""
    if not name or not options:
        return name
    import re as _re
    try:
        from backend.purchase.services.variation import korean_label as _kl
    except Exception:
        _kl = lambda x: None
    vals = set()
    for o in options:
        for dim in ("color", "size_label", "flavor_attr", "style", "split_value"):
            v = o.get(dim) if isinstance(o, dict) else None
            if v and str(v).strip() and str(v).strip() != "_unknown":
                raw = str(v).strip()
                vals.add(raw)
                try:
                    kl = _kl(raw)
                    if kl:
                        vals.add(str(kl).strip())
                except Exception:
                    pass
    out = name
    for v in sorted(vals, key=len, reverse=True):
        if len(v) < 2:
            continue
        out = _re.sub(r'(?<![가-힣A-Za-z0-9])' + _re.escape(v) + r'(?![가-힣A-Za-z0-9])', ' ', out, flags=_re.IGNORECASE)
    out = _re.sub(r'\s*[,\|·/]\s*', ' ', out)           # 남은 구분자 정리
    out = _re.sub(r'\(\s*,\s*', '(', out); out = _re.sub(r'\s*,\s*\)', ')', out)
    out = _re.sub(r'\(\s*[,\s·]*\)', '', out)           # 빈 괄호 제거
    _li, _ri = out.rfind('('), out.rfind(')')
    if _li > _ri:                                        # 미완성 열림괄호(잘림) 제거
        out = out[:_li].rstrip()
    out = _re.sub(r'\s+', ' ', out).strip(" ,-·|()").strip()
    return out if len(out) >= 5 else name               # 과도 제거 방지


def _child_original_price(child_pid, child_asin, sale_price):
    # ★그룹 옵션별 정상가(2026-07-06): products.original_price_krw 읽고 NULL이면 SP-API로 lazy 채움.
    #   정상가/판매가 모두 calculate_sale_krw 산출 → 정상가>=판매가 자연 성립. 실패시 판매가=정상가.
    opk = None
    try:
        if child_pid:
            from backend.purchase.database import get_db
            with get_db() as _c:
                _row = _c.execute("SELECT original_price_krw FROM products WHERE id=?", (child_pid,)).fetchone()
            opk = _row[0] if _row else None
            if not opk and child_asin:
                from backend.purchase.services.dual_pricing import refresh_dual_price
                _r = refresh_dual_price(child_pid, child_asin)
                if _r:
                    opk = _r["original_krw"]
        opk = int(opk) if opk else int(sale_price)
    except Exception:
        opk = int(sale_price)
    return opk if opk >= int(sale_price) else int(sale_price)


import re as _re_disamb


def _detect_hand_kor(title):
    """제목에서 손 축 감지 → 왼손/오른손/None (골프장갑 등)."""
    t = (title or "").lower()
    if _re_disamb.search(r"worn on (the )?left|left[-\s]?hand(ed)?|\blh\b|left glove|\ub9c8\uc678\uc190|\uc67c\uc190|\uc88c\uc218", t):
        return "\uc67c\uc190"  # 왼손
    if _re_disamb.search(r"worn on (the )?right|right[-\s]?hand(ed)?|\brh\b|right glove|\uc624\ub978\uc190|\uc6b0\uc218", t):
        return "\uc624\ub978\uc190"  # 오른손
    return None


_DISAMB_STOP = {"the", "a", "an", "for", "and", "with", "of", "in", "on", "to", "by",
                "set", "pack", "piece", "pieces", "size", "sizes", "men", "women", "mens",
                "womens", "unisex", "new", "pro", "premium", "amazon", "count", "ct", "pk"}


def _distinct_title_token(title, others):
    """title 에서 others 에 없는 의미있는 토큰 1개 추출(한글화). 없으면 None."""
    from backend.purchase.services.variation import korean_label
    def _toks(sv):
        return [w for w in _re_disamb.findall(r"[A-Za-z\uac00-\ud7a3]+", (sv or "").lower())
                if len(w) >= 2 and w not in _DISAMB_STOP]
    mine = _toks(title)
    other = set()
    for o in others:
        other |= set(_toks(o))
    for w in mine:
        if w not in other:
            kw = korean_label(w) or w
            kw = str(kw).strip()
            if kw and not kw.isdigit() and 1 <= len(kw) <= 12:
                return kw
    return None


def _disambiguate_option_labels(valid, dim_groups):
    """옵션 라벨 중복 시 의미있는 구별자 부여. 반환: idx -> 최종 라벨.
    우선순위: ①왼손/오른손(손 축) ②제목 차이 토큰 ③(최후) 숫자."""
    from backend.purchase.database import get_db
    from collections import defaultdict
    base = [_option_label_for_child(ch, dim_groups) for (ch, pr) in valid]
    asins = [(ch.get("asin") or "") for (ch, pr) in valid]
    titles = {}
    _a = [a for a in asins if a]
    if _a:
        try:
            with get_db() as conn:
                q = "SELECT asin, title_en, title_ko FROM products WHERE asin IN (%s)" % ",".join(["?"] * len(_a))
                for r in conn.execute(q, _a).fetchall():
                    titles[r["asin"]] = r["title_en"] or r["title_ko"] or ""
        except Exception:
            pass
    buckets = defaultdict(list)
    for i, b in enumerate(base):
        buckets[b].append(i)
    final = list(base)
    for b, idxs in buckets.items():
        if len(idxs) < 2:
            continue
        for pos, i in enumerate(idxs):
            t = titles.get(asins[i], "")
            hand = _detect_hand_kor(t)
            if hand:
                final[i] = b + " " + hand
                continue
            others = [titles.get(asins[j], "") for j in idxs if j != i]
            tok = _distinct_title_token(t, others)
            final[i] = (b + " " + tok) if tok else (b + " " + str(pos + 1))
    return final


def build_coupang_payload(
    group: dict, split: dict, pricing_for_split: list[dict],
    *, requested: bool = True, force_no_brand: bool = False,
    fast_detail: bool = False, max_options: int | None = None,
    reasons: list | None = None, category_override: str | None = None,
) -> Optional[dict]:
    """multi-option 쿠팡 sellerProducts 페이로드.

    fast_detail=True: 옵션별 AI 상세 렌더 생략(디테일링 분리, 빠른 임시저장).
    max_options=N: 옵션 N개로 상한(괴물 그룹 꼬리 컷, 처리량 절감).

    requested=True: 등록 동시 자동 승인요청(심사대기열 진입).
    requested=False: 임시저장 상태(셀러센터에서 검토·삭제 가능). 테스트·점진 운영에 권장.

    items 배열 N개 — 각 child 가 1개 vendorItem.
    """
    options = split.get("options") or []
    if not options:
        return None
    # ★마스터 후보 = de-contam 통과 자식만(pricing_for_split=register에서 오염제거됨).
    #   이름/카테고리를 살아남은 주력 클러스터에서 뽑아 '이름-옵션 불일치' 방지(Fammart 선반↔캐디).
    _decon = {p.get("child_asin") for p in pricing_for_split if p.get("child_asin")}
    _opts_clean = [o for o in options if o.get("asin") in _decon] or options
    # master = options 중 coupang_category_code 보유 child 우선, 없으면 첫 child
    master = None
    meta = None
    product = None
    for o in _opts_clean:
        m = _load_master_meta(o.get("asin"))
        if m.get("product") and m.get("coupang_category_code"):
            master = o; meta = m; product = m["product"]; break
    if not master:
        for o in _opts_clean:
            m = _load_master_meta(o.get("asin"))
            if m.get("product"):
                master = o; meta = m; product = m["product"]; break
    if not master:
        logger.warning(f"[group-lister] split '{split.get('name')}' — products 테이블에 children 0건")
        return _pl_fail(reasons, "[group-lister] split '…' — products 테이블에 children 0건")
    if not product:
        logger.warning(f"[group-lister-coupang] master {master_asin} products 없음")
        return _pl_fail(reasons, "[group-lister-coupang] master … products 없음")

    # 한국 마켓 표시용 → title_ko 우선(AI 번역 결과). 없으면 split.name, 최후 title_en.
    # ★ [브랜드명] placeholder 제거 + brand 영문 prefix 보강 (= coupang 단독 path 와 동일 규칙).
    from backend.purchase.services.coupang_lister import build_seller_product_name as _bspn
    name = _bspn(
        title_ko=product.get("title_ko"),
        brand=product.get("brand"),
        title_en=product.get("title_en"),
        fallback_name=split.get("name"),
        max_len=80,
    )
    if len(name) < 5:
        logger.warning(f"[group-lister-coupang] master {master_asin} sellerProductName 빌드 실패({name!r})")
        return _pl_fail(reasons, "[group-lister-coupang] master … sellerProductName 빌드 실패(…)")
    # ★ 브랜드 — NEW 계정은 쿠팡 라이브러리 brandId 해석(2026 정책), OLD는 기존 정규화.
    from backend.purchase.services.brand_normalizer import normalize_brand as _norm_brand
    from backend.purchase.services.coupang_lister import (
        _resolve_brand_ko, _resolve_model_no, _resolve_brand_new, _gtin_attribute,
    )
    try:
        from backend.purchase.services.coupang_service import active_account as _aa; _CACT = _aa()
    except Exception:
        _CACT = "old"
    _grp_brand_id = None
    _grp_uid_required = False
    if _CACT == "new":
        _gbn = {"brand_id": None, "brand_name": "", "uid_required": False} if force_no_brand \
            else _resolve_brand_new(group.get("brand") or "")
        # ★UID필수 브랜드는 그룹에서 노브랜드 폴백 — 그룹은 자식마다 GTIN 보장 불가
        #   (자식 다수가 바코드 없음 → GTIN누락 시 승인반려). 단일경로는 자식별 바코드 체크로 처리.
        if _gbn["brand_id"] and not _gbn["uid_required"]:
            _nb = _gbn["brand_name"] or (group.get("brand") or "")
            _grp_brand_id = _gbn["brand_id"]
            _grp_uid_required = _gbn["uid_required"]
        else:
            _nb = ""   # brandId 없음/UID필수 → 노브랜드 (brandId 없이 브랜드명만 보내면 쿠팡 거부, 2026-07-08 실측)
    else:
        _nb = _norm_brand(group.get("brand") or "")
    # ★상품명 B(좁은 브랜드 음역, 2026-07-01): name이 브랜드로 시작 + 관용영문 아니면 앞 브랜드 한글 음역.
    try:
        from backend.purchase.services.coupang_lister import apply_brand_translit_to_name as _abt
        name = _abt(name, group.get("brand") or "")
    except Exception:
        pass
    # ★그룹 상품명에서 옵션값(색상/사이즈 등) 제거 — 쿠팡 옵션 자동노출 중복경고 방지(2026-07-01).
    name = _strip_group_option_words(name, options)
    # ★사이즈 suffix는 required_names(카테고리 MANDATORY) 확인 후 아래에서 조건부 적용(2026-07-01).
    #   사이즈가 옵션축(MANDATORY)이면 옵션으로 자동노출돼 이름 suffix "(5 Lb)"가 중복 → 생략.
    if len(name) > 80:
        name = name[:80].rstrip()   # 쿠팡 sellerProductName 100자, 70~80자 권장
    if not name:
        return _pl_fail(reasons, "[group-lister-coupang] 상품명 빌드 결과가 빈 문자열")
    category_code = meta.get("coupang_category_code")
    # ★2026-08-03: extend(기존 상품에 옵션 추가) 시에는 쿠팡이 이미 그 상품의 카테고리를
    #   알고 있다. 우리 DB 는 95.5% 가 비어 있어 category="0" → required_attrs=[] →
    #   "필수 구매 옵션 존재하지 않습니다" 로 상품 전체가 거부됐다(실측 144건).
    #   기존 상품의 displayCategoryCode 를 물려받아 필수속성을 제대로 생성한다.
    if category_override:
        category_code = category_override
    # ★ 2026-06-03: 신규 패밀리는 listings_pa 카테고리가 없어 cat=0 → 노이즈-drop 으로
    # 그룹화 실패했음. 단일 경로와 동일한 키워드캐시 + RAG 예측으로 카테고리 확보.
    if not category_code and product:
        category_code = _resolve_group_category(product, name)
    category = str(category_code) if category_code else "0"   # 자동매칭 fallback

    by_asin = {p["child_asin"]: p for p in pricing_for_split}
    valid = [(o, by_asin.get(o.get("asin"))) for o in options if by_asin.get(o.get("asin"))]
    if not valid:
        return _pl_fail(reasons, "[group-lister-coupang] pricing 매칭된 유효옵션 0건 (가격산출 실패)")
    # ★ 목록통관 면세 한도 — 원가 $150 초과 옵션 제외(관세 발생, 구매대행 부적합). 단일경로 게이트의 옵션판.
    from backend.purchase.services.coupang_lister import _exceeds_customs_limit, CUSTOMS_DUTY_FREE_USD
    _pre_n = len(valid)
    valid = [(o, pr) for (o, pr) in valid if not _exceeds_customs_limit((pr or {}).get("cost_usd"))]
    if len(valid) < _pre_n:
        logger.info(f"[group-customs] {group.get('parent_asin')} cat={category} — 원가>${int(CUSTOMS_DUTY_FREE_USD)} 옵션 {_pre_n - len(valid)}건 제외(관세)")
    if not valid:
        logger.info(f"[group-customs] {group.get('parent_asin')} — 관세초과로 옵션 0건, 빌드 거부")
        return _pl_fail(reasons, "[group-customs] … — 관세초과로 옵션 0건, 빌드 거부")
    # ★신 파이프라인 M9.7b 선정 존중 (2026-08-12)
    #   import_option 이 있으면 **우리가 고른 child** 로 제한한다.
    #   max_options 는 단순 앞자르기(valid[:N])라 우리 선정 기준
    #   (①has_offer ②가격밴드 ③옵션명 충돌 ④대표+seq)과 전혀 다른 것을 남긴다.
    #   ★import_option 이 없으면(구 파이프라인) 종전과 100% 동일 — 회귀 0.
    _pa = group.get("parent_asin")
    if _pa:
        try:
            # ★get_db 는 모듈 전역이 아니다 — 이 파일은 함수 안에서 지역 import 한다
            from backend.purchase.database import get_db as _get_db
            with _get_db() as _c:
                # ★채널을 반드시 건다 — import_option 은 채널별 행이다.
                #   parent 로만 조회하면 채널별 선정이 갈릴 때 합집합이 되어
                #   그 채널에서 안 고른 child 까지 통과한다.
                _keep = {r["child_asin"] for r in _c.execute(
                    "SELECT child_asin FROM import_option WHERE parent_asin=? AND channel=?",
                    (_pa, "coupang"))}
            if _keep:
                _pre = len(valid)
                _sel = [(o, pr) for (o, pr) in valid if o.get("asin") in _keep]
                if _sel:
                    valid = _sel
                    logger.info(f"[group-m9] {_pa} 옵션 {_pre}→{len(valid)} — M9.7b 선정 적용")
                else:
                    # ★우리 선정이 하나도 안 남으면 등록하지 않는다.
                    #   앞자르기로 아무거나 올리면 M9 를 통과 못 한 것이 나간다.
                    return _pl_fail(reasons,
                                    f"[group-m9] {_pa} — M9.7b 선정 child 가 유효옵션에 없다"
                                    f"(선정 {len(_keep)} · 유효 {_pre})")
        except Exception as _e:  # noqa: BLE001
            logger.warning(f"[group-m9] {_pa} import_option 조회 실패(계속): {str(_e)[:80]}")

    # ★ 옵션 상한 — 괴물 그룹(수십 옵션) 꼬리 컷(처리량·관리 절감). 앞 N개 유지.
    if max_options and len(valid) > max_options:
        logger.info(f"[group-cap] {group.get('parent_asin')} 옵션 {len(valid)}→{max_options} 상한컷")
        valid = valid[:max_options]

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
    from backend.purchase.services.coupang_attributes import build_required_attributes
    from backend.purchase.services.coupang_lister import _normalize_search_tags
    meta_cat = get_category_meta(category) if category != "0" else None
    required_attrs = get_required_attributes(category) if category != "0" else []
    required_names = {a.get("attributeTypeName") for a in required_attrs if a.get("attributeTypeName")}
    # ★사이즈 suffix 조건부 적용(2026-07-01): 사이즈가 카테고리 옵션축(MANDATORY '사이즈'/'크기' 포함)이면
    #   쿠팡이 옵션으로 자동노출 → 이름 suffix "(5 Lb)"가 옵션과 중복(경고) → 생략. 옵션축 아닐 때만 구분용 suffix.
    _sv = split.get("split_value")
    if split.get("split_dim") == "size" and _sv and _sv != "_unknown":
        _cat_mand = {a.get("attributeTypeName") for a in (meta_cat.get("attributes") or [])
                     if a.get("required") == "MANDATORY"} if meta_cat else set()
        _size_is_axis = any(("사이즈" in n or "크기" in n) for n in _cat_mand if n)
        if not _size_is_axis:
            from backend.purchase.services.variation import korean_label as _kl
            _sv_kor = _kl(_sv) or _sv
            if _sv_kor and _sv_kor not in name:
                _suffix = f" ({_sv_kor})"
                _budget = 80 - len(_suffix)
                if len(name) > _budget:
                    name = name[:_budget].rstrip()
                name = f"{name}{_suffix}"
    # cat_path — 단일경로와 동일 소스(coupang_categories). build_required_attributes 의
    # 카테고리 특례(식품>건강식품 자동생성옵션 등) 판단에 필요.
    cat_path = ""
    if category != "0":
        from backend.purchase.database import get_db as _get_db
        with _get_db() as _conn:
            _cpr = _conn.execute(
                "SELECT path FROM coupang_categories WHERE code=? LIMIT 1", (category,)
            ).fetchone()
            cat_path = (_cpr["path"] if _cpr else "") or ""
    # ★어린이/키즈 완화(2026-07-01): 비-어린이 카테고리면 부수 '어린이'→'온가족용/전연령'(KC 오탐). 완구류는 유지.
    try:
        from backend.purchase.services.coupang_lister import soften_kids_terms as _skt
        name = _skt(name, cat_path)
    except Exception:
        pass
    # master facts — Loop A C1 fallback (child 옵션값 비면 master 재사용)
    master_facts = None
    for c in group.get("children") or []:
        if c.get("asin") == master.get("asin"):
            master_facts = c
            break

    dim_groups = _extract_option_values(split)
    if not dim_groups:
        return _pl_fail(reasons, "[group-lister-coupang] 옵션값 추출 실패 (_extract_option_values 빈값) — 변형축 값 부재")
    try:
        _final_labels = _disambiguate_option_labels(valid, dim_groups)  # ★중복라벨 의미분해
    except Exception as _de:
        logger.warning(f"[disamb] 실패, 기본라벨 폴백 {group.get('parent_asin')}: {repr(_de)}")
        _final_labels = [_option_label_for_child(_ch, dim_groups) for (_ch, _pr) in valid]

    # 변형차원 → 카테고리 실제 옵션축(MANDATORY 속성명) 매핑.
    # 쿠팡은 카테고리가 인정하는 속성만 옵션 구분축으로 사용. 속성명이 안 맞으면(예: 조리도구엔
    # '색상'이 없고 '색상계열') 그 옵션이 무시돼 남은 동일 속성으로 "중복된 옵션값" 거부 발생.
    _cat_mand_names = {a.get("attributeTypeName") for a in (meta_cat.get("attributes") or [])
                       if a.get("required") == "MANDATORY" and a.get("attributeTypeName")} if meta_cat else set()
    _DIM_SYN = {
        "색상": ("색상", "색상계열", "색상/디자인", "컬러"),
        "사이즈": ("사이즈", "크기"),
        "스타일": ("스타일",),
        "용량": ("용량",),
        "맛": ("맛", "향"),
        # 확장 axes (number_of_items, package_quantity, material, model_number 대응)
        "수량": ("수량", "개수", "개당 수량"),
        "패키지수량": ("패키지수량", "패키지수", "구성수량", "수량"),
        "재질": ("재질", "소재", "재질구분"),
        "모델명": ("모델명", "모델", "모델번호", "품번"),
    }
    def _map_dim(group_name: str) -> Optional[str]:
        syns = _DIM_SYN.get(group_name, (group_name,))
        # 1) 정확 일치 우선
        for cand in syns:
            if cand in _cat_mand_names:
                return cand
        # 2) 부분 일치 — 카테고리 속성명이 동의어를 포함하는 경우
        #    (의류 '패션의류/잡화 사이즈', 신발 '신발사이즈', '색상계열' 등).
        #    단 스펙성 속성(최대커버사이즈·호환 지름 등)은 제품 변형축이 아니므로 제외.
        _spec_exclude = ("최대", "커버", "호환", "지름", "권장", "적정")
        for name in _cat_mand_names:
            if any(x in name for x in _spec_exclude):
                continue
            if any(cand in name for cand in syns):
                return name
        return None
    _dim_axis = {dg["groupName"]: _map_dim(dg["groupName"]) for dg in dim_groups}
    # 2026-06-05 수량 차원 매핑 — 아마존이 style 등으로 라벨한 팩-수량 차원(1 Pack/3 Packs)을
    # 카테고리 수량 MANDATORY 옵션축에 연결. 미연결 시 (색상,사이즈) 중복 붕괴 -> 단일 폴백.
    _qty_dim_name = None
    _qty_axis = _quantity_axis_name(_cat_mand_names)
    if _qty_axis and _qty_axis not in {v for v in _dim_axis.values() if v}:
        for _dg in dim_groups:
            if _dim_axis.get(_dg["groupName"]):
                continue
            _rvs = _dg.get("raw_values") or []
            _hits = sum(1 for _rv in _rvs if _quantity_value(_rv))
            if _rvs and _hits >= max(1, (len(_rvs) + 1) // 2):
                _dim_axis[_dg["groupName"]] = _qty_axis
                _qty_dim_name = _dg["groupName"]
                break
    if meta_cat and not any(_dim_axis.values()):
        logger.info(
            f"[group-lister-coupang] {group.get('parent_asin')} cat={category} — 변형차원 "
            f"{list(_dim_axis)} 가 카테고리 옵션축(MANDATORY {_cat_mand_names})과 매칭 안 됨 → 옵션화 불가"
    )
        return _pl_fail(reasons, "[group-lister-coupang] … cat=… — 변형차원 … 가 카테고리 옵션축(MANDATORY …)과 매칭 안 됨 → 옵션화 불가")

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

    noisy_skip_count = 0
    # ★ 교차색상 공용이미지 집합 — 여러 색상 자식이 공유하는 색상무관 generic 이미지(화이트 옵션에
    #   흑색 "10 PACK" generic 등). ★image_cache 의 photo-분류 이미지 원본ID 기준 — facts images 는
    #   generic 공유본을 누락해 부정확. 그룹당 1회 계산 후 group 에 캐시(split 반복 호출 비용 절감).
    _shared_imgids = group.get("_shared_imgids_cache")
    if _shared_imgids is None:
        from backend.purchase.services.coupang_lister import _amazon_img_id as _aimg
        _imgid_colors = {}
        try:
            from backend.purchase.services.image_classifier import classify_images as _cls_imgs
            from backend.purchase.services.coupang_lister import _image_policy as _ipol
            from backend.purchase.database import get_db as _gdb_img
            for _gc in (group.get("children") or []):
                _gcol = (_gc.get("color") or "").strip().lower().replace("grey", "gray")
                _gpid = (_load_master_meta(_gc.get("asin")).get("product") or {}).get("id") if _gc.get("asin") else None
                if not (_gcol and _gpid):
                    continue
                # ★2026-08-03: 비전분류(Gemini)는 화장품/건기식(self_made)에만 쓴다.
                #   공유 판정 자체는 "같은 이미지ID가 2색상 이상에 등장"하는 집합 연산이라 AI 가 필요 없다.
                #   photo 필터는 마케팅컷 제외용인데, 일반상품은 마케팅컷이 공유로 잡혀 제외돼도 무방하다.
                #   (전 품목 비전 호출로 캐시적중 5.5%·제미나이 비용 대부분이 여기서 발생했다)
                _gcls = _cls_imgs(_gpid) if _ipol(_gpid) == "self_made" else None
                with _gdb_img() as _gconn:
                    _grows = _gconn.execute("SELECT local_path, original_url FROM image_cache WHERE product_id=?", (_gpid,)).fetchall()
                for _gr in _grows:
                    if _gcls is None or _gcls.get(_gr["local_path"]) == "photo":
                        _gii = _aimg(_gr["original_url"] or "")
                        if _gii:
                            _imgid_colors.setdefault(_gii, set()).add(_gcol)
        except Exception as _e:
            logger.warning(f"[group-img] {group.get('parent_asin')} 교차색상 공용이미지 계산 실패: {_e}")
        _shared_imgids = {iid for iid, cols in _imgid_colors.items() if len(cols) >= 2}
        group["_shared_imgids_cache"] = _shared_imgids
        if _shared_imgids:
            logger.info(f"[group-img] {group.get('parent_asin')} 교차색상 공용이미지 {len(_shared_imgids)}개 제외대상")
    _seen_item_names = {}
    # ★ 색상별 대표 이미지(형제 차용용) — 같은 색상 자식 중 색상전용 photo 보유한 첫 자식의 이미지.
    #   막다른 자식(공유 generic만, 예: 화이트인데 흑색 hero만)에 같은 색상 형제 이미지 차용.
    _child_colors = group.get("_child_colors_cache")
    _color_repimg = group.get("_color_repimg_cache")
    if _child_colors is None:
        from backend.purchase.services.coupang_lister import _get_product_images as _gpi_rep
        _child_colors, _color_repimg = {}, {}
        for _gc in (group.get("children") or []):
            _gcol = (_gc.get("color") or "").strip().lower().replace("grey", "gray")
            _gas = _gc.get("asin")
            if not (_gcol and _gas):
                continue
            _child_colors[_gas] = _gcol
            if _gcol not in _color_repimg:
                _gpid2 = (_load_master_meta(_gas).get("product") or {}).get("id")
                if _gpid2:
                    _sprep = _gpi_rep(_gpid2, _shared_imgids, strict_exclude=True)
                    if _sprep:
                        _color_repimg[_gcol] = _sprep[:9]
        group["_child_colors_cache"] = _child_colors
        group["_color_repimg_cache"] = _color_repimg
    _shared_ed_urls = None  # ★그룹 공유 에디토리얼(1회 렌더)
    for idx, (child, pr) in enumerate(valid):
        attributes = []
        from backend.purchase.services.variation import korean_label, clean_axis_value
        provided_names = set()
        _skip_child_noisy = False

        def _default_for(name: str) -> str:
            if "스타일" in name: return "기본"
            if "사이즈" in name or "크기" in name: return "원사이즈"
            if "색상" in name: return "기타"
            if "맛" in name or "향" in name: return "기본"
            return "기본"

        for dg in dim_groups:
            cat_attr = _dim_axis.get(dg["groupName"])
            if meta_cat and not cat_attr:
                continue  # 이 카테고리의 옵션축이 아닌 변형차원은 옵션 속성으로 넣지 않음
            attr_name = cat_attr or dg["groupName"]  # meta 없음(auto-match)이면 원래 이름 사용
            raw = child.get(dg["key"])
            # C1 fallback: child 비면 master 의 같은 attribute (color, size 등) 재사용
            if not raw and master_facts:
                raw = master_facts.get(dg["key"])
            if dg["groupName"] == _qty_dim_name:
                # 수량 축 — N Pack(s) 등을 N개로 변환 (clean_axis_value 노이즈 처리 우회)
                val = _quantity_value(raw) or "1개"
            else:
                # ★ axis 값 정규화 — strip 노이즈 + 한글 통일 + fuzzy 오타 정정.
                #   결과 None 이면 옵션 의미 없음 → child 전체 skip.
                cleaned = clean_axis_value(raw, max_len=24) if raw else None
                if raw and cleaned is None:
                    _skip_child_noisy = True
                    break
                val = cleaned or (dg.get("value_map", {}).get(raw) or korean_label(raw) or raw)
                if not val:
                    val = _default_for(dg["groupName"])
            attributes.append({
                "attributeTypeName": attr_name,
                "attributeValueName": val[:24],
                "exposed": "EXPOSED",
                "editable": True,
            })
            provided_names.add(attr_name)
        if _skip_child_noisy:
            noisy_skip_count += 1
            continue

        # ★ "사이즈" 안전망 — split.split_value (auto_split primary_value) 를 모든 옵션 사이즈
        # attr 으로 강제 fill. split 안 모든 child 가 사이즈 동일이라 _extract_option_values 는
        # 사이즈를 dim 으로 안 잡음. 그러나 카테고리 mandatory 가 "사이즈" 면 누락 시 셀러센터
        # 빨간 필수 + 검증 실패. category=0(자동매칭) 등록 시 mandatory 모르므로 무조건 fill —
        # mandatory 아닌 카테고리에선 쿠팡이 extra attr 로 무시.
        _size_axis = "사이즈"
        for _cand in ("패션의류/잡화 사이즈", "신발사이즈", "크기"):
            if _cand in _cat_mand_names:
                _size_axis = _cand
                break
        if _size_axis not in provided_names and split.get("split_dim") == "size":
            _size_val = split.get("split_value")
            if _size_val and _size_val != "_unknown":
                from backend.purchase.services.variation import korean_label as _kl
                attributes.append({
                    "attributeTypeName": _size_axis,
                    "attributeValueName": (_kl(_size_val) or _size_val)[:24],
                    "exposed": "EXPOSED",
                    "editable": True,
                })
                provided_names.add(_size_axis)

        # 카테고리 mandatory 자동 채움 — 검증된 단일경로 추출기(build_required_attributes) 재사용.
        # 옵션 child 각각이 product 이므로 단일상품과 동일하게 속성 추출(Tier0 저장값→Tier2 AI→Tier1 정규식).
        # 식품>건강식품 등 자동생성옵션 카테고리는 [] 반환 → 단일경로와 일관. 변형차원(색/사이즈)은
        # 위 Loop A 가 이미 채웠으므로 provided_names 로 중복 방지.
        child_product = _load_master_meta(child.get("asin")).get("product") or {}
        if meta_cat and child_product:
            child_attrs, _attr_skip = build_required_attributes(meta_cat, child_product, cat_path=cat_path)
            for ca in child_attrs:
                cname = ca.get("attributeTypeName")
                if cname and cname not in provided_names:
                    _av = ca.get("attributeValueName")
                    if isinstance(_av, str) and len(_av) > 30:
                        ca["attributeValueName"] = _av[:30]
                    attributes.append(ca)
                    provided_names.add(cname)

        # ★ "수량/패키지수량" 최종 폴백 — build_required_attributes 가 못 채운 경우만 "1개"(식품=1병 등).
        #   세트 실팩수량(100개입 등)은 build_required_attributes 가 _extract_quantity 로 이미 채웠으므로
        #   여기 도달 안 함. (이전엔 이 블록이 루프 위에 있어 "1개"를 선점→정답 차단하던 버그, 2026-06-20)
        for _qty_name in ("수량", "패키지수량"):
            if _qty_name in _cat_mand_names and _qty_name not in provided_names:
                attributes.append({
                    "attributeTypeName": _qty_name,
                    "attributeValueName": "1개",
                    "exposed": "EXPOSED",
                    "editable": True,
                })
                provided_names.add(_qty_name)

        # 검색태그 — 단일경로와 동일. 없으면 로테이션 AI 생성, 4개 전멸이면 그룹등록 보류(2026-07-12)
        from backend.purchase.services.coupang_lister import _ensure_seo_tags as _est
        _seo_g, _seo_gblk = _est(child_product.get("id"), child_product.get("seo_tags"),
                                 child_product.get("title_ko") or child_product.get("title_en"), cat_path)
        if _seo_gblk:
            raise RuntimeError("AI 소진(GPT+Gemini) — 그룹 검색어 생성 불가로 등록 보류")
        item_search_tags = _normalize_search_tags(_seo_g, group.get("brand") or "")

        # ★옵션 라벨 — 중복 시 의미있는 구별자(왼손/오른손·제목차이) 사전계산(2026-07-08)
        item_name = _final_labels[idx] if idx < len(_final_labels) else _option_label_for_child(child, dim_groups)
        # 안전: 사전계산 후에도 남는 충돌은 숫자 폴백
        _dup_n = _seen_item_names.get(item_name, 0)
        if _dup_n:
            item_name = item_name + " " + str(_dup_n + 1)
        _seen_item_names[item_name] = _dup_n + 1
        # ★ 2026-06-03: 옵션별 이미지 — 각 child 자체 호스팅 이미지 우선(없으면 다운로드+리사이즈),
        # 끝내 없으면 master image_urls 폴백. 쿠팡은 아마존 원본 URL 거부 → 호스팅 리사이즈분만 사용.
        # (이전엔 모든 옵션이 master 이미지 공유 → 18팩 옵션에 2팩 이미지 노출 문제)
        from backend.purchase.services.coupang_lister import _get_product_images as _coupang_images
        _cpid = child_product.get("id")
        child_imgs = []
        if _cpid:
            # 1차: 색상전용 photo (교차색상 공유 generic 제외, strict)
            child_imgs = _coupang_images(_cpid, _shared_imgids, strict_exclude=True) or []
            if not child_imgs and child_product.get("images_json"):
                try:
                    import asyncio as _aio_img
                    from backend.purchase.services.image_downloader import download_product_images
                    _aio_img.run(download_product_images(_cpid, child_product["images_json"]))
                    child_imgs = _coupang_images(_cpid, _shared_imgids, strict_exclude=True) or []
                except Exception as _e:
                    logger.warning(f"[group-img] child {child.get('asin')} 이미지 호스팅 실패: {_e}")
        # 2차: 색상전용 0장(공유 generic만) → 같은 색상 형제 이미지 차용 (화이트 옵션 흑색 hero 방지)
        if not child_imgs:
            _ccol = _child_colors.get(child.get("asin"))
            if _ccol and _color_repimg.get(_ccol):
                child_imgs = _color_repimg[_ccol]
                logger.info(f"[group-img] {child.get('asin')} 색상전용 없음 → '{_ccol}' 형제 이미지 차용")
        # 3차: 그래도 없으면 비-strict(공유/마케팅 포함 대표 폴백), 최후 master image_urls
        if not child_imgs and _cpid:
            child_imgs = _coupang_images(_cpid, _shared_imgids) or []
        per_item_urls = (child_imgs or image_urls)[:9]
        # ★ 갤러리 = 누끼형 대표 1장만(단일경로 정책, 추가 갤러리 미사용 — 상세 contents가 제품이미지 커버).
        #   옵션(child)별 색상전용 대표컷. 생성 실패 시 기존 다수(첫 REPRESENTATION + DETAIL) 폴백.
        from backend.purchase.services.coupang_lister import (
            select_all_nuki_images as _snuki, select_representative_image as _srep,
            _image_policy as _imgpol, _raw_amazon_images as _rawimg)
        # ── 이미지 정책 분기(2026-07-06): 화장품/건강식품=누끼 / 그외=아마존 원본 ──
        if _imgpol(_cpid) == "amazon":
            _graw = _rawimg(_cpid, 9) or list(per_item_urls)
            item_images = [{"imageOrder": _ri, "imageType": "REPRESENTATION" if _ri == 0 else "DETAIL", "vendorPath": _ru}
                           for _ri, _ru in enumerate(_graw)]
        else:
            _nuki_urls = []
            try:
                _nuki_urls = _snuki(_cpid, exclude_original_ids=_shared_imgids, strict_exclude=True) if _cpid else []
            except Exception as _e:
                logger.warning(f"[group-img] {child.get('asin')} 멀티누끼 실패(폴백): {_e}")
                _nuki_urls = []
            if _nuki_urls:
                item_images = [{"imageOrder": 0, "imageType": "REPRESENTATION", "vendorPath": _nuki_urls[0]}]
                for _gi, _gu in enumerate(_nuki_urls[1:], start=1):
                    item_images.append({"imageOrder": _gi, "imageType": "DETAIL", "vendorPath": _gu})
            else:
                _rep_url = None
                try:
                    _rep_url = _srep(_cpid) if _cpid else None
                except Exception as _e:
                    logger.warning(f"[group-img] {child.get('asin')} 대표컷 생성 실패(폴백): {_e}")
                if _rep_url:
                    item_images = [{"imageOrder": 0, "imageType": "REPRESENTATION", "vendorPath": _rep_url}]
                else:
                    item_images = []
                    for i, url in enumerate(per_item_urls):
                        item_images.append({
                            "imageOrder": i,
                            "imageType": "REPRESENTATION" if i == 0 else "DETAIL",
                            "vendorPath": url,
                        })

        # ★ 상세 contents — 단일경로와 동일 템플릿(브랜드배너→선별제품컷+인포그래픽→스펙표→정보배너).
        #   옵션(child)별로 그 child_product 자체 상세 생성(저작권 방어). _cpid 없으면 기존 이미지+배너 폴백.
        if _cpid:
            from backend.purchase.services.coupang_lister import build_detail_contents as _bdc
            # ★옵션별 풍부 설계 상세 (2026-06-30): fast=False 강제 — select_detail_images(자식 전체풀,
            #   마케팅만 제외)로 풍부한 제품컷 + 인포그래픽 + 스펙표. 공유제외 per_item_urls는 select
            #   실패시 폴백으로만. select/infographic 은 캐시기반(라이브 Gemini 아님)→캡 무관.
            #   세트면 구성품 가공컷 생성(≥2종일 때만, 비세트는 즉시 None).
            try:
                from backend.purchase.services.components_image import ensure_components_cut as _ecc
                _ecc(_cpid)
            except Exception: pass
            # 상세용 이미지 풀 = 공유제외 안 한 전체 게이트통과 제품컷(풍부). 썸네일/옵션구분은
            # per_item_urls(공유제외) 유지. select_detail_images 가 부족하면 이 풀로 보충됨.
            _detail_pool = _coupang_images(_cpid) or per_item_urls
            if _imgpol(_cpid) == "amazon":
                item_contents = _bdc(_cpid, _detail_pool, fast=True, shared_editorial=None)
            else:
                if not _shared_ed_urls:
                    _shared_ed_urls = _gen_group_shared_editorial(_cpid)
                item_contents = _bdc(_cpid, _detail_pool, fast=True, shared_editorial=_shared_ed_urls)
        else:
            item_contents = [
                {"contentsType": "IMAGE_NO_SPACE",
                 "contentDetails": [{"content": u, "detailType": "IMAGE", "altText": ""}]}
                for u in per_item_urls[:10]
            ] + _banner_contents
        # GTIN/바코드 — 단일 경로와 동일하게 자식 identifiers_json 에서 추출.
        # (2026-06-03: 그룹 경로 emptyBarcode 하드코딩 수정 — 단일은 2026-05-25에 이미 고쳐짐)
        from backend.purchase.services.coupang_lister import _barcode_with_facts as _xbc
        _item_bc = _xbc(child.get("asin"), child_product.get("identifiers_json"),
                        child_product.get("id"))
        items.append({
            "itemName": item_name,
            "originalPrice": (_child_original_price(_cpid, child.get("asin"), int(pr["sale_krw"])) if _cpid else int(pr["sale_krw"] * 1.2)),
            "salePrice": int(pr["sale_krw"]),
            "maximumBuyCount": 100,
            "maximumBuyForPerson": 0,
            "maximumBuyForPersonPeriod": 1,
            "outboundShippingTimeDay": 4,   # 2026-06-03 5→4 (운영 정책)
            "unitCount": 1,
            "adultOnly": "EVERYONE",
            "taxType": "TAX",
            "parallelImported": "NOT_PARALLEL_IMPORTED",
            "overseasPurchased": "OVERSEAS_PURCHASED",
            "pccNeeded": True,
            "externalVendorSku": child.get("asin"),
            "barcode": _item_bc,
            "emptyBarcode": not bool(_item_bc),
            "emptyBarcodeReason": "" if _item_bc else "COUPANG",
            # 식별번호 정책(2026-08-01): GTIN(barcode) 없으면 modelNo(품번) 필수. SP-API 코드형/ASIN.
            "modelNo": ("" if _item_bc else _resolve_model_no(child.get("asin"))),
            "extraProperties": {},
            "certifications": [],
            "searchTags": item_search_tags,
            "offerCondition": "NEW",
            "stockQuantity": 100,
            "saleStartedAt": sale_started_at,
            "saleEndedAt": sale_ended_at,
            "displayProductName": item_name,
            "brand": _nb,
            "manufacture": _nb,
            "images": item_images,
            # UID필수 브랜드면 GTIN attribute 추가(2026 신규 필수속성).
            "attributes": (list(attributes or []) + [_gtin_attribute(_item_bc)]
                           if (_grp_uid_required and _item_bc) else attributes),
            "notices": build_default_notices(meta_cat) if meta_cat else [],
            "contents": item_contents,
        })

    if noisy_skip_count:
        logger.info(
            f"[group-lister-coupang] {group.get('parent_asin')} cat={category} — "
            f"노이즈 옵션 {noisy_skip_count}건 drop"
        )
    if not items:
        logger.info(
            f"[group-lister-coupang] {group.get('parent_asin')} cat={category} — "
            f"노이즈 후 옵션 0건 (모두 drop), 빌드 거부"
    )
        return _pl_fail(reasons, "[group-lister-coupang] … cat=… — 노이즈 후 옵션 0건 (모두 drop), 빌드 거부")

    # 옵션축 붕괴 검사 — 두 옵션의 (속성명,값) 집합이 동일하면 쿠팡 "중복된 옵션값" 거부.
    # 제품이 다차원 변형이나 카테고리가 일부 차원만 옵션축으로 인정해 미인정 차원이 붕괴한 경우.
    if len(items) >= 2:
        sigs = [tuple(sorted((a["attributeTypeName"], a["attributeValueName"]) for a in it["attributes"]))
                for it in items]
        if len(set(sigs)) < len(sigs):
            # ★ 붕괴 시 거부 대신 dedup — 같은 시그니처는 대표 1개만 유지(상품 살림, 2026-06-21).
            #   주문은 대표 옵션 externalVendorSku(ASIN)로 정확히 추적(합쳐진 나머지는 주문불가).
            #   노이즈중복(Black/Black 3/black 4=동일툴) 안전. ★같은색 다른무늬는 대표만=불일치 소지 → 추후 2축(스타일) 분리.
            _seen_sig = set()
            _dedup_items = []
            for _it, _sig in zip(items, sigs):
                if _sig not in _seen_sig:
                    _seen_sig.add(_sig)
                    _dedup_items.append(_it)
            if len(_dedup_items) >= 2:
                logger.info(
                    f"[group-lister-coupang] {group.get('parent_asin')} cat={category} — "
                    f"시그니처 붕괴 {len(items)}→{len(_dedup_items)} dedup(중복색 대표만 유지)"
                )
                items = _dedup_items
            else:
                logger.info(
                    f"[group-lister-coupang] {group.get('parent_asin')} cat={category} — "
                    f"붕괴→유효옵션 1개뿐(단일상품) → 멀티옵션 불가, skip(단일폴백 대상)"
    )
                return _pl_fail(reasons, "[group-lister-coupang] … cat=… — 붕괴→유효옵션 1개뿐(단일상품) → 멀티옵션 불가, skip(단일폴백 대상)")

    from backend_shared._config import (
        COUPANG_VENDOR_ID, COUPANG_USER_ID,
        COUPANG_OUTBOUND_SHIPPING_PLACE_CODE, COUPANG_RETURN_CENTER_CODE,
    )
    # ★ 계정-인식 식별자/코드 — 정적 상수는 import 시점 COUPANG_ACTIVE 로 고정돼 멀티계정
    #   (coupang_account("new")) 컨텍스트를 무시하는 버그가 있었음(=OLD_OWNED 오인 원인). _vendor()류로 해소.
    from backend.purchase.services.coupang_service import (
        _vendor as _acc_vendor, _user_id as _acc_user,
        _outbound_code as _acc_out, _return_center as _acc_ret)
    _VID = _acc_vendor()
    _UID = _acc_user()
    _OUTBOUND = _acc_out() or COUPANG_OUTBOUND_SHIPPING_PLACE_CODE
    _RETCENTER = _acc_ret() or COUPANG_RETURN_CENTER_CODE
    _master = {
        "sellerProductName": name,
        "displayCategoryCode": int(category) if category.isdigit() else 0,
        "vendorId": _VID,
        "saleStartedAt": sale_started_at,
        "saleEndedAt": sale_ended_at,
        "displayProductName": name,
        "brand": _nb,
        "manufacture": _nb,
        "deliveryMethod": "AGENT_BUY",
        "deliveryCompanyCode": "CJGLS",
        "deliveryChargeType": "FREE",
        "deliveryCharge": 0,
        "freeShipOverAmount": 0,
        "deliveryChargeOnReturn": 9000,
        "remoteAreaDeliverable": "N",
        "unionDeliveryType": "NOT_UNION_DELIVERY",
        "returnCenterCode": _RETCENTER,
        "returnChargeName": "Charis G",
        "companyContactNumber": "010-8558-7277",
        "returnZipCode": "01425",
        "returnAddress": "서울특별시 도봉구 해등로 24",
        "returnAddressDetail": "(반품 받는 주소 — 추후 환경별 보정)",
        "returnCharge": 9000,
        "outboundShippingPlaceCode": int(_OUTBOUND) if str(_OUTBOUND).isdigit() else 0,
        "vendorUserId": _UID or _VID,
        "requested": bool(requested),   # True=자동승인요청 / False=임시저장
        "items": items,
        "requiredDocumentNames": [],
        "extraInfoMessage": "",
        "manufacture": _nb,
    }
    # ★신규계정 brandId(2026 정책) — 라이브러리 매칭시만 첨부.
    if _grp_brand_id:
        _master["brandId"] = _grp_brand_id
    return _master


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
