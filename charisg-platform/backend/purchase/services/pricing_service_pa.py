"""PA 가격 산정 서비스 — 채널별 목표 마진 역산.

공식:
  cost_krw_var = (cost_usd + amazon_shipping_usd + cj_shipping_usd) * fx
  cost_krw_fix = safety_margin_krw + cs_cost_krw
  denom        = 1 - target_margin_rate - channel_fee_rate - return_reserve_pct
  sale_krw_raw = (cost_krw_var + cost_krw_fix) / denom
  sale_krw     = round(sale_krw_raw / 100) * 100   # 100원 단위

신규 파라미터(safety/cs/return)는 default 0 으로 하위호환 보장.
호출자가 명시하면 비용 반영, 안 하면 기존 동작 유지.

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
_AMAZON_SHIP_KEY = "amazon_shipping_default_usd"


def _get_setting_float(key: str, default: float | None = None) -> float:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
    if not row or row["value"] is None or row["value"] == "":
        if default is not None:
            return default
        raise ValueError(f"settings 에 {key} 누락")
    return float(row["value"])


def _get_channel_fee_rate(channel: str) -> float:
    return _get_setting_float(_FEE_KEY[channel])


def calculate_sale_krw(
    cost_usd: float,
    amazon_shipping_usd: float | None = None,
    cj_shipping_usd: float = 0.0,
    channel: str = "smartstore",
    target_margin_override: float | None = None,
    # 신규 (default 0 → 기존 호출자 영향 없음):
    safety_margin_krw: float = 0.0,
    cs_cost_krw: float = 0.0,
    return_reserve_pct: float = 0.0,
) -> dict:
    """채널별 목표 마진 역산.

    Args:
        cost_usd:              Amazon 매입가
        amazon_shipping_usd:   Amazon 직배송비. None 이면 settings default. Forwarder
                               경로 호출자는 0.0 명시 권장 (이중 차감 방지).
        cj_shipping_usd:       배대지 운송비. Forwarder 경로에서 forwarder_shipping_usd()
                               LBS 요금표 결과 전달.
        channel:               'smartstore' | 'coupang'
        target_margin_override: 채널/카테고리별 다른 마진 시. 없으면 settings.
        safety_margin_krw:     안전마진(처리비). default 0.
        cs_cost_krw:           CS 비용. default 0. Forwarder 전용.
        return_reserve_pct:    리턴 적립률 (0.03 = 3%). default 0. Forwarder 전용.

    Returns:
        dict — sale_krw + cost breakdown (실측 net_margin_krw 포함).
    """
    if channel not in _ALLOWED_CHANNELS:
        raise ValueError(
            f"unknown channel: {channel} (allowed: {_ALLOWED_CHANNELS})"
        )
    if amazon_shipping_usd is None:
        amazon_shipping_usd = _get_setting_float(_AMAZON_SHIP_KEY, default=0.0)
    if cost_usd < 0 or amazon_shipping_usd < 0 or cj_shipping_usd < 0:
        raise ValueError("cost/shipping 값은 0 이상이어야 함")
    if safety_margin_krw < 0 or cs_cost_krw < 0 or return_reserve_pct < 0:
        raise ValueError("safety/cs/return 값은 0 이상이어야 함")

    fx = get_current_rate()
    fee_rate = _get_channel_fee_rate(channel)
    target_margin = (
        target_margin_override
        if target_margin_override is not None
        else _get_setting_float(_DEFAULT_TARGET_MARGIN_KEY)
    )

    denom = 1.0 - target_margin - fee_rate - return_reserve_pct
    if denom <= 0:
        raise ValueError(
            f"infeasible margin: target_margin({target_margin}) + "
            f"fee_rate({fee_rate}) + return_pct({return_reserve_pct}) >= 1.0"
        )

    cost_usd_total = cost_usd + amazon_shipping_usd + cj_shipping_usd
    cost_krw_var = cost_usd_total * fx
    cost_krw_fix = safety_margin_krw + cs_cost_krw

    sale_krw_raw = (cost_krw_var + cost_krw_fix) / denom
    sale_krw = int(round(sale_krw_raw / 100) * 100)

    # 실측 비용 분해 (sale 확정 후)
    fee_krw = sale_krw * fee_rate
    return_krw = sale_krw * return_reserve_pct
    net_margin_krw = (
        sale_krw - cost_krw_var - fee_krw - return_krw
        - safety_margin_krw - cs_cost_krw
    )

    return {
        "channel": channel,
        "exchange_rate": fx,
        "cost_usd_total": round(cost_usd_total, 2),
        "cost_krw": int(cost_krw_var),               # 기존 호환: 변동 비용 합
        "fee_rate": fee_rate,
        "fee_krw": int(fee_krw),
        "target_margin_rate": target_margin,
        "net_margin_krw": int(net_margin_krw),       # ★ 실측 (가정 아님)
        "sale_krw": sale_krw,
        # 신규 breakdown
        "amazon_shipping_usd": round(amazon_shipping_usd, 2),
        "cj_shipping_usd": round(cj_shipping_usd, 2),
        "safety_margin_krw": int(safety_margin_krw),
        "cs_krw": int(cs_cost_krw),
        "return_pct": return_reserve_pct,
        "return_krw": int(return_krw),
    }
