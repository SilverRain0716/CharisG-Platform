"""
margin_calculator.py — PA 마진 계산기.

셀러 마진 = 판매가
          − (아마존 매입 + 아마존→KR 배송) × 환율
          − 채널 수수료 (sale × channel_fee_rate)
          − 배대지 처리비 (forwarder_fee_krw)
          − 반품충당 (sale × return_reserve_pct)
          − CS비 (cs_cost_krw)
          − 도메스틱 배송비 (배대지 → 고객. 기본 셀러 부담)
고객 총 비용 = 판매가 + 도메스틱 배송비 + 예상 관부가세
"""
from dataclasses import dataclass
from typing import Optional

from backend.purchase.database import get_db


_FEE_KEY = {
    "smartstore": "smartstore_fee_rate",
    "coupang": "coupang_fee_rate",
}


@dataclass
class MarginInput:
    amazon_price_usd: float
    sale_price_krw: float
    fx_rate: float = 1380.0
    amazon_shipping_usd: float = 0.0      # Amazon→KR 배송비 (Direct $8.45)
    channel: str = "smartstore"           # smartstore|coupang — channel_fee_rate 결정용
    channel_fee_rate: float = 0.0         # 채널 수수료율 (settings 자동 로드)
    forwarder_fee_krw: float = 5000.0
    return_reserve_pct: float = 3.0       # 판매가 %
    cs_cost_krw: float = 2000.0
    domestic_shipping_krw: float = 3000.0
    customs_duty_krw: float = 0.0
    quantity: int = 1


@dataclass
class MarginResult:
    cost_krw: float                       # (USD 매입 + 배송) × 환율
    amazon_shipping_krw: float
    channel_fee_krw: float
    forwarder_fee_krw: float
    return_reserve_krw: float
    cs_cost_krw: float
    seller_net_krw: float                 # 셀러 순익
    seller_margin_pct: float              # 순익 / 판매가
    customer_total_krw: float             # 고객 총 비용


def _load_defaults() -> dict:
    """settings 테이블에서 기본값 로드."""
    keys = (
        "margin.forwarder_fee_krw",
        "margin.return_reserve_pct",
        "margin.cs_cost_krw",
        "margin.default_fx_rate",
        "amazon_shipping_default_usd",
        "smartstore_fee_rate",
        "coupang_fee_rate",
    )
    out = {}
    with get_db() as conn:
        for k in keys:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
            if row and row["value"] not in (None, ""):
                out[k] = row["value"]
    return out


def calculate(inp: MarginInput) -> MarginResult:
    amazon_shipping_krw = inp.amazon_shipping_usd * inp.fx_rate * inp.quantity
    cost_krw = inp.amazon_price_usd * inp.fx_rate * inp.quantity + amazon_shipping_krw
    channel_fee_krw = inp.sale_price_krw * inp.channel_fee_rate
    return_reserve_krw = inp.sale_price_krw * (inp.return_reserve_pct / 100.0)

    seller_net = (
        inp.sale_price_krw
        - cost_krw
        - channel_fee_krw
        - inp.forwarder_fee_krw
        - return_reserve_krw
        - inp.cs_cost_krw
    )
    margin_pct = (seller_net / inp.sale_price_krw * 100.0) if inp.sale_price_krw else 0.0
    customer_total = inp.sale_price_krw + inp.domestic_shipping_krw + inp.customs_duty_krw

    return MarginResult(
        cost_krw=round(cost_krw, 0),
        amazon_shipping_krw=round(amazon_shipping_krw, 0),
        channel_fee_krw=round(channel_fee_krw, 0),
        forwarder_fee_krw=inp.forwarder_fee_krw,
        return_reserve_krw=round(return_reserve_krw, 0),
        cs_cost_krw=inp.cs_cost_krw,
        seller_net_krw=round(seller_net, 0),
        seller_margin_pct=round(margin_pct, 2),
        customer_total_krw=round(customer_total, 0),
    )


def calculate_with_defaults(
    amazon_price_usd: float,
    sale_price_krw: float,
    customs_duty_krw: float = 0.0,
    channel: str = "smartstore",
) -> MarginResult:
    defaults = _load_defaults()
    fee_key = _FEE_KEY.get(channel, _FEE_KEY["smartstore"])
    return calculate(MarginInput(
        amazon_price_usd=amazon_price_usd,
        sale_price_krw=sale_price_krw,
        fx_rate=float(defaults.get("margin.default_fx_rate", 1380)),
        amazon_shipping_usd=float(defaults.get("amazon_shipping_default_usd", 0)),
        channel=channel,
        channel_fee_rate=float(defaults.get(fee_key, 0)),
        forwarder_fee_krw=float(defaults.get("margin.forwarder_fee_krw", 5000)),
        return_reserve_pct=float(defaults.get("margin.return_reserve_pct", 3)),
        cs_cost_krw=float(defaults.get("margin.cs_cost_krw", 2000)),
        customs_duty_krw=customs_duty_krw,
    ))


def save_margin(sourcing_id: int, inp: MarginInput, result: MarginResult,
                competition: str = None) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO margin_calcs
               (sourcing_id, amazon_price_usd, fx_rate, forwarder_fee_krw,
                return_reserve_krw, cs_cost_krw, sale_price_krw,
                domestic_shipping_krw, customs_duty_krw,
                customer_total_krw, seller_net_krw, seller_margin_pct, competition)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sourcing_id, inp.amazon_price_usd, inp.fx_rate, inp.forwarder_fee_krw,
             result.return_reserve_krw, inp.cs_cost_krw, inp.sale_price_krw,
             inp.domestic_shipping_krw, inp.customs_duty_krw,
             result.customer_total_krw, result.seller_net_krw, result.seller_margin_pct,
             competition),
        )
    return cur.lastrowid
