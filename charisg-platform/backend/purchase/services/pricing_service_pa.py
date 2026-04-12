"""PA 가격 산정 서비스 — 채널별 목표 마진 역산.

공식:
  cost_krw     = (cost_usd + amazon_shipping_usd + cj_shipping_usd) * fx
  denom        = 1 - target_margin_rate - channel_fee_rate
  sale_krw_raw = cost_krw / denom
  sale_krw     = round(sale_krw_raw / 100) * 100   # 100원 단위

채널 수수료는 settings 테이블에서 로드 (하드코딩 금지).
환율은 exchange_rate_service.get_current_rate() 가 sanity 보장.
"""
from backend.purchase.database import get_db
from backend.purchase.services.exchange_rate_service import get_current_rate

_ALLOWED_CHANNELS = ("smartstore", "coupang")
_FEE_KEY = {
    "smartstore": "smartstore_fee_rate",
    "coupang": "coupang_fee_rate",
}
_DEFAULT_TARGET_MARGIN_KEY = "margin_target_rate"


def _get_setting_float(key: str) -> float:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
    if not row or row["value"] is None or row["value"] == "":
        raise ValueError(f"settings 에 {key} 누락")
    return float(row["value"])


def _get_channel_fee_rate(channel: str) -> float:
    return _get_setting_float(_FEE_KEY[channel])


def calculate_sale_krw(
    cost_usd: float,
    amazon_shipping_usd: float = 0.0,
    cj_shipping_usd: float = 0.0,
    channel: str = "smartstore",
    target_margin_override: float | None = None,
) -> dict:
    if channel not in _ALLOWED_CHANNELS:
        raise ValueError(
            f"unknown channel: {channel} (allowed: {_ALLOWED_CHANNELS})"
        )
    if cost_usd < 0 or amazon_shipping_usd < 0 or cj_shipping_usd < 0:
        raise ValueError("cost/shipping 값은 0 이상이어야 함")

    fx = get_current_rate()
    fee_rate = _get_channel_fee_rate(channel)
    target_margin = (
        target_margin_override
        if target_margin_override is not None
        else _get_setting_float(_DEFAULT_TARGET_MARGIN_KEY)
    )

    denom = 1.0 - target_margin - fee_rate
    if denom <= 0:
        raise ValueError(
            f"infeasible margin: target_margin({target_margin}) + "
            f"fee_rate({fee_rate}) >= 1.0"
        )

    cost_usd_total = cost_usd + amazon_shipping_usd + cj_shipping_usd
    cost_krw_float = cost_usd_total * fx
    sale_krw_raw = cost_krw_float / denom
    sale_krw = int(round(sale_krw_raw / 100) * 100)
    net_margin_krw = int(sale_krw * target_margin)
    fee_krw = int(sale_krw * fee_rate)

    return {
        "channel": channel,
        "exchange_rate": fx,
        "cost_usd_total": round(cost_usd_total, 2),
        "cost_krw": int(cost_krw_float),
        "fee_rate": fee_rate,
        "fee_krw": fee_krw,
        "target_margin_rate": target_margin,
        "net_margin_krw": net_margin_krw,
        "sale_krw": sale_krw,
    }
