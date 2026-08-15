# -*- coding: utf-8 -*-
"""아마존 정가(정상가) + 현재 할인가(판매가) → 쿠팡 정상가/판매가 계산·저장 (2026-07-07).
★컬럼 우선(SP-API 0콜): products.amazon_price_usd(정가)/landed_price_usd(현재)가 있으면 그걸로 즉시 계산.
   둘 다 없을 때만 SP-API get_item_offers 폴백(allow_api=True). get_item_offers는 rate-limit이 심해
   그룹 자식마다 호출하면 throttle로 건당 100s+ → 컬럼 우선으로 대부분 0콜 처리해 병목 제거.
products.original_price_krw(정상가) + sale_price_krw(판매가) 갱신. 컬럼 저장이라 나중에 SP-API 없이 수정 가능."""
import os
import time
import logging
from datetime import datetime, timezone

from backend.purchase.database import get_db
from backend.purchase.services.pricing_service_pa import calculate_sale_krw
from backend.purchase.services.forwarder_shipping import forwarder_shipping_usd
from backend.purchase.services.channel_listing_service import _load_default_forwarder_extras

logger = logging.getLogger(__name__)
_client = None


def _prod_client():
    global _client
    if _client is None:
        from sp_api.api import Products
        from sp_api.base import Marketplaces
        from backend.dropshipping.services.amazon_sp_api_service import get_credentials
        _client = Products(credentials=get_credentials(), marketplace=Marketplaces.US)
    return _client


def _fetch_offers(asin, retries=4):
    for i in range(retries):
        try:
            return _prod_client().get_item_offers(asin=asin, item_condition="New").payload
        except Exception as e:
            msg = str(e)
            if "QuotaExceeded" in msg or "429" in msg or "Throttl" in msg:
                time.sleep(1.5 * (i + 1))
                continue
            logger.warning(f"[dual_price] get_item_offers 실패 {asin}: {msg[:80]}")
            return None
    return None


def _offers_to_usd(pl):
    """SP-API payload → (list_usd 정가, cur_usd 현재)."""
    s = pl.get("Summary", {}) or {}
    list_usd = (s.get("ListPrice") or {}).get("Amount")
    cur_usd = None
    for bb in s.get("BuyBoxPrices") or []:
        if str(bb.get("condition", "")).lower() == "new":
            cur_usd = (bb.get("LandedPrice") or {}).get("Amount")
            break
    if cur_usd is None:
        for o in pl.get("Offers") or []:
            lp = (o.get("ListingPrice") or {}).get("Amount")
            sh = (o.get("Shipping") or {}).get("Amount") or 0
            if lp:
                cur_usd = lp + sh
                break
    return list_usd, cur_usd


def _store(product_id, list_usd, cur_usd, channel, write_usd):
    """정가/현재 USD → KRW 계산·저장. write_usd=True면 amazon/landed USD 컬럼도 갱신(SP-API 경로)."""
    if not cur_usd:
        return None
    if not list_usd or list_usd < cur_usd:
        list_usd = cur_usd   # 할인 없음 → 정상가=판매가
    try:
        # ★배송비(배대지)+고정비 포함 — 누락 시 배송비만큼 마진 부풀려짐(2026-07-11 수정)
        with get_db() as _wc:
            _wr = _wc.execute("SELECT weight_g FROM products WHERE id=?", (product_id,)).fetchone()
        _fw = forwarder_shipping_usd(_wr["weight_g"] if _wr else None)
        _ex = _load_default_forwarder_extras()
        _kw = dict(amazon_shipping_usd=0.0, cj_shipping_usd=_fw, channel=channel,
                   safety_margin_krw=_ex["safety_krw"], cs_cost_krw=_ex["cs_krw"],
                   return_reserve_pct=_ex["return_pct"])
        sale_krw = int((calculate_sale_krw(cost_usd=float(cur_usd), **_kw) or {}).get("sale_krw") or 0)
        orig_krw = int((calculate_sale_krw(cost_usd=float(list_usd), **_kw) or {}).get("sale_krw") or 0)
    except Exception as e:
        logger.warning(f"[dual_price] calc 실패 {product_id}: {e}")
        return None
    if sale_krw < 1000:
        return None
    if orig_krw < sale_krw:
        orig_krw = sale_krw
    disc = round((list_usd - cur_usd) / list_usd * 100, 2) if list_usd else 0.0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with get_db() as conn:
            if write_usd:
                conn.execute(
                    "UPDATE products SET sale_price_krw=?, original_price_krw=?, amazon_price_usd=?, "
                    "landed_price_usd=?, listing_price_usd=?, discount_pct=?, price_fetched_at=? WHERE id=?",
                    (sale_krw, orig_krw, list_usd, cur_usd, cur_usd, disc, now, product_id))
            else:
                # 컬럼 우선 경로: 이미 신뢰된 amazon/landed USD는 건드리지 않고 KRW/할인만 갱신
                conn.execute(
                    "UPDATE products SET sale_price_krw=?, original_price_krw=?, discount_pct=? WHERE id=?",
                    (sale_krw, orig_krw, disc, product_id))
            conn.commit()
    except Exception as e:
        logger.warning(f"[dual_price] 저장 실패 {product_id}: {e}")
        return None
    return {"sale_krw": sale_krw, "original_krw": orig_krw,
            "list_usd": list_usd, "cur_usd": cur_usd, "discount_pct": disc}


def refresh_dual_price(product_id, asin, channel="coupang", allow_api=True):
    """정상가/판매가 계산·저장. ①amazon/landed USD 컬럼 우선(0콜) ②없으면 SP-API(allow_api).
    성공 dict / 실패 None."""
    # ★PA_DUAL_NO_API=1 이면 SP-API 폴백 금지(컬럼만). 마이그 대량등록 시 throttle 병목 제거용.
    #   컬럼 없는 상품은 None 반환 → 리스터가 기존 sale_price_krw를 정상가=판매가로 사용(할인 미표시).
    #   가격은 별도 백필잡(backfill_dual_price.py, allow_api=True)이 나중에 채움.
    if os.environ.get("PA_DUAL_NO_API") == "1":
        allow_api = False
    # ① 컬럼 우선 — amazon_price_usd(정가)/landed_price_usd(현재) 이미 백필돼 있으면 SP-API 없이 즉시 계산
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT amazon_price_usd, landed_price_usd FROM products WHERE id=?",
                (product_id,)).fetchone()
    except Exception:
        row = None
    if row:
        list_usd = row["amazon_price_usd"]
        cur_usd = row["landed_price_usd"]
        # 현재가만 있고 정가 없으면 정가=현재(할인0), 정가만 있고 현재 없으면 현재=정가
        if cur_usd or list_usd:
            r = _store(product_id, list_usd, cur_usd or list_usd, channel, write_usd=False)
            if r:
                return r
    # ② SP-API 폴백 (컬럼이 비어있는 경우에만)
    if not allow_api or not asin:
        return None
    pl = _fetch_offers(asin)
    if not pl:
        return None
    list_usd, cur_usd = _offers_to_usd(pl)
    return _store(product_id, list_usd, cur_usd, channel, write_usd=True)
