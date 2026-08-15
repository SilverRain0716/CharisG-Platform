"""
pricing_service.py — 가격 정책 엔진
소싱가 → 판매가 자동 계산 (정책 적용)

계산 공식:
  기본가 = 소싱가(원화) × markup_rate + fixed_addition
  관부가세 = 기본가 × tax_rate (KR only, auto일 때 10%)
  마켓수수료 = (기본가 + 관부가세) × market_fee_rate
  배송비 = shipping_fee (별도일 때)
  판매가 = 기본가 + 관부가세 + 마켓수수료 + 배송비 → 끝자리 올림
  마진 = 판매가 - 소싱가(원화) - 관부가세 - 마켓수수료 - 배송비
"""
import math
import logging
import time
from typing import Optional
from backend_shared.context import get_db

logger = logging.getLogger(__name__)

# 환율 캐시 (1시간 TTL)
_exchange_cache = {"rate": None, "fetched_at": 0}
CACHE_TTL = 3600  # 1시간


def get_exchange_rate(source: str = "USD", target: str = "KRW") -> float:
    """실시간 환율 조회 (캐시 적용)"""
    now = time.time()
    if _exchange_cache["rate"] and (now - _exchange_cache["fetched_at"]) < CACHE_TTL:
        return _exchange_cache["rate"]

    try:
        import requests
        # exchangerate-api.com 무료 플랜 (월 1,500회)
        url = f"https://open.er-api.com/v6/latest/{source}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("result") == "success":
            rate = data["rates"].get(target, 1380.0)
            _exchange_cache["rate"] = rate
            _exchange_cache["fetched_at"] = now
            logger.info(f"환율 업데이트: 1 {source} = {rate} {target}")
            return rate
    except Exception as e:
        logger.warning(f"환율 API 실패, 기본값 사용: {e}")

    return _exchange_cache.get("rate") or 1380.0  # fallback


def calculate_price(
    source_price: float,
    source_currency: str = "USD",
    policy_id: Optional[int] = None,
    policy: Optional[dict] = None,
) -> dict:
    """
    소싱가 → 판매가 계산

    Returns:
        {
            "source_price": 15.0,
            "source_price_local": 20700,      # 소싱가(원화)
            "exchange_rate": 1380.0,
            "base_price": 51750,              # 기본가 (마크업 적용)
            "tax_amount": 5175,               # 관부가세
            "fee_amount": 3128,               # 마켓수수료
            "shipping_fee": 0,                # 배송비
            "final_price": 60100,             # 최종 판매가 (올림)
            "margin": 30225,                  # 마진
            "margin_pct": 50.3,               # 마진율 (%)
        }
    """
    # 정책 로드
    if policy is None and policy_id:
        policy = _load_policy(policy_id)
    if policy is None:
        policy = _default_policy()

    # 환율
    if policy.get("exchange_rate_auto") or policy.get("exchange_rate_auto") is None:
        exchange_rate = get_exchange_rate(
            source_currency,
            policy.get("target_currency", "KRW")
        )
    else:
        exchange_rate = policy.get("exchange_rate") or 1380.0

    # 소싱가 원화 환산
    source_local = source_price * exchange_rate

    # 기본가 = 소싱가(원화) × 마크업 + 고정추가금
    markup = policy.get("markup_rate", 2.5)
    fixed = policy.get("fixed_addition", 0)
    base_price = source_local * markup + fixed

    # 관부가세
    tax_type = policy.get("tax_type", "auto")
    if tax_type == "none" or policy.get("market", "KR") == "US":
        tax_amount = 0
    elif tax_type == "manual":
        tax_rate = policy.get("tax_rate", 0) / 100
        tax_amount = base_price * tax_rate
    else:  # auto
        tax_amount = base_price * 0.10  # 일반 관부가세 10%

    # 마켓 수수료
    fee_rate = policy.get("market_fee_rate", 0) / 100
    fee_amount = (base_price + tax_amount) * fee_rate

    # 배송비
    shipping_type = policy.get("shipping_type", "included")
    if shipping_type == "separate":
        shipping_fee = policy.get("shipping_fee", 0)
    elif shipping_type == "conditional":
        threshold = policy.get("free_shipping_threshold", 0)
        shipping_fee = 0 if base_price >= threshold else policy.get("shipping_fee", 0)
    else:  # included
        shipping_fee = 0

    # 판매가 = 기본가 + 세금 + 수수료 (배송비는 별도 표시)
    raw_price = base_price + tax_amount + fee_amount

    # 끝자리 올림
    rounding = int(policy.get("price_rounding", "100"))
    if rounding > 0:
        final_price = math.ceil(raw_price / rounding) * rounding
    else:
        final_price = round(raw_price)

    # 최소/최대 가격 제한
    min_p = policy.get("min_price")
    max_p = policy.get("max_price")
    if min_p and final_price < min_p:
        final_price = min_p
    if max_p and final_price > max_p:
        final_price = max_p

    # 마진 계산
    total_cost = source_local + tax_amount + fee_amount + shipping_fee
    margin = final_price - total_cost
    margin_pct = (margin / final_price * 100) if final_price > 0 else 0

    return {
        "source_price": source_price,
        "source_currency": source_currency,
        "source_price_local": round(source_local),
        "exchange_rate": exchange_rate,
        "markup_rate": markup,
        "base_price": round(base_price),
        "tax_amount": round(tax_amount),
        "fee_amount": round(fee_amount),
        "shipping_fee": round(shipping_fee),
        "raw_price": round(raw_price),
        "final_price": int(final_price),
        "margin": round(margin),
        "margin_pct": round(margin_pct, 1),
        "target_currency": policy.get("target_currency", "KRW"),
    }


def check_margin_threshold(margin_pct: float, policy_id: Optional[int] = None) -> bool:
    """최소 마진율 충족 여부"""
    if policy_id:
        policy = _load_policy(policy_id)
        min_margin = policy.get("min_margin_pct", 25)
    else:
        min_margin = 25
    return margin_pct >= min_margin


def _load_policy(policy_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM pricing_policies WHERE id = ?", (policy_id,)).fetchone()
    return dict(row) if row else _default_policy()


def _default_policy() -> dict:
    return {
        "market": "KR",
        "target_currency": "KRW",
        "exchange_rate_auto": True,
        "markup_rate": 2.5,
        "fixed_addition": 0,
        "tax_type": "auto",
        "tax_rate": 0,
        "market_fee_rate": 5.5,
        "shipping_type": "included",
        "shipping_fee": 0,
        "price_rounding": "100",
        "min_margin_pct": 25,
    }
