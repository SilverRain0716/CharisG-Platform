"""마진 게이트 — 등록 직전 net_margin_krw 평가 및 차단.

사용자 정책: net_margin_krw < 3,000 → DANGER (등록 차단).
공식은 [[scripts/margin_monitor]] 와 동일 — 현재원가(listing_price_usd > landed_price_usd > cost_usd)
기준 cost_krw + 쿠팡 수수료 + 배대지 + 반품 적립 + CS 비용 차감.
"""
from __future__ import annotations
import logging
from backend.purchase.database import get_db

logger = logging.getLogger(__name__)

MIN_MARGIN_KRW = 3_000


def _load_settings(conn) -> dict:
    keys = (
        "coupang_fee_rate", "margin.forwarder_fee_krw", "margin.return_reserve_pct",
        "margin.cs_cost_krw", "margin.default_fx_rate",
    )
    d: dict = {}
    for k in keys:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
        if r and r[0] not in (None, ""):
            try:
                d[k] = float(r[0])
            except ValueError:
                pass
    return {
        "fee": d.get("coupang_fee_rate", 0.11),
        "forwarder": d.get("margin.forwarder_fee_krw", 5000.0),
        "return_pct": d.get("margin.return_reserve_pct", 0.0),
        "cs": d.get("margin.cs_cost_krw", 2000.0),
        "fx": d.get("margin.default_fx_rate", 1380.0),
    }


def evaluate_listing_margin(
    product_id: int,
    min_margin_krw: int = MIN_MARGIN_KRW,
    channel: str = "coupang",
) -> tuple[str, int | None, str]:
    """listings_pa + products 기반 실마진 평가.

    Returns:
        (status, net_margin_krw, reason)
        status ∈ {"OK", "DANGER", "STALE"}
        - OK: net_margin_krw >= min_margin_krw
        - DANGER: net_margin_krw < min_margin_krw  ← 등록 차단 대상
        - STALE: 평가 불가 (sale_krw 0 또는 cost 모두 NULL)
    """
    with get_db() as conn:
        s = _load_settings(conn)
        r = conn.execute(
            """SELECT l.sale_krw, p.cost_usd, p.listing_price_usd, p.shipping_usd, p.landed_price_usd
               FROM listings_pa l JOIN products p ON p.id = l.product_id
               WHERE l.product_id=? AND l.channel=? LIMIT 1""",
            (product_id, channel),
        ).fetchone()
    if not r:
        return ("STALE", None, "listings_pa 행 없음")
    sale = r["sale_krw"] or 0
    if sale <= 0:
        return ("STALE", None, "sale_krw=0")

    if r["listing_price_usd"] and r["listing_price_usd"] > 0:
        item, ship = r["listing_price_usd"], (r["shipping_usd"] or 0)
    elif r["landed_price_usd"] and r["landed_price_usd"] > 0:
        item, ship = r["landed_price_usd"], 0.0
    elif r["cost_usd"] and r["cost_usd"] > 0:
        item, ship = r["cost_usd"], 0.0
    else:
        return ("STALE", None, "cost_usd / landed / listing 모두 NULL")

    cost_krw = item * s["fx"] + ship * s["fx"]
    seller_net = (
        sale - cost_krw - sale * s["fee"] - s["forwarder"]
        - sale * (s["return_pct"] / 100.0) - s["cs"]
    )
    net_krw = round(seller_net)
    if net_krw < min_margin_krw:
        return (
            "DANGER", net_krw,
            f"net_margin={net_krw:,}원 < 임계 {min_margin_krw:,}원 (sale={int(sale):,}원, cost_krw={int(cost_krw):,}원)",
        )
    return ("OK", net_krw, f"net_margin={net_krw:,}원")


def block_listing_if_low_margin(
    product_id: int, channel: str = "coupang", min_margin_krw: int = MIN_MARGIN_KRW,
) -> tuple[bool, str]:
    """등록 직전 호출. DANGER 면 listings_pa.status='excluded' 마킹 + (True, reason) 반환.

    Returns:
        (blocked, reason)
        - blocked=True: 차단됨 (caller 가 등록 중단)
        - blocked=False: 통과 (OK 또는 STALE — STALE 은 통과시켜 사후 monitor 가 잡음)
    """
    status, net, reason = evaluate_listing_margin(product_id, min_margin_krw, channel)
    if status != "DANGER":
        return (False, reason)
    with get_db() as conn:
        conn.execute(
            """UPDATE listings_pa SET status='excluded',
                     margin_status='DANGER', margin_checked_at=CURRENT_TIMESTAMP,
                     net_margin_krw=COALESCE(?, net_margin_krw),
                     error_message=?
               WHERE product_id=? AND channel=?""",
            (net, f"마진 차단 ({reason})", product_id, channel),
        )
    logger.warning(f"[margin-gate] product {product_id} 차단: {reason}")
    return (True, reason)
