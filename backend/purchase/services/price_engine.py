# -*- coding: utf-8 -*-
"""가격 엔진 v2 — price_mode(auto/manual_fixed/manual_base) + 할인추종 + 히스테리시스 손실가드 + 가드3종.

설계서: acct_work/price_engine_design.md
순수 계산 + DB 읽기만. 쿠팡 PUT은 호출자(reprice 잡)가 수행 → 이 모듈 import 자체는 라이브 무영향.

evaluate(product, listing, live_price) → 결정 dict:
  {target, mode, guard_state(new), reason, sanity_flag, margin, cost_usd, cost_src, needs_put}
"""
from backend.purchase.database import get_db
from backend.purchase.services.pricing_service_pa import (
    get_current_rate, _get_channel_fee_rate, _get_setting_float,
)
from backend.purchase.services.forwarder_shipping import forwarder_shipping_usd
from backend.purchase.services.channel_listing_service import _load_default_forwarder_extras


def load_config(channel: str = "coupang") -> dict:
    ex = _load_default_forwarder_extras()  # safety_krw, cs_krw, return_pct (라이브 일치)
    return {
        "channel": channel,
        "fx": get_current_rate(),
        "fee": _get_channel_fee_rate(channel),
        "margin": _get_setting_float("margin_target_rate", 0.35),
        "return_pct": ex["return_pct"],
        "safety": ex["safety_krw"],
        "cs": ex["cs_krw"],
        "floor": _get_setting_float("price.floor_krw", 10000.0),
        "deadband": _get_setting_float("price.guard_deadband_krw", 5000.0),
        "sanity_markup": _get_setting_float("price.sanity_markup", 3.0),
        "put_threshold": _get_setting_float("price.put_threshold_krw", 500.0),
        "flash_days": int(_get_setting_float("price.flash_lookback_days", 14)),
        "flash_ratio": _get_setting_float("price.flash_ratio", 0.8),
    }


def _round100(x: float) -> int:
    return int(round(x / 100.0) * 100)


def _cost_parts(cost_usd, weight_g, cfg):
    fwd = forwarder_shipping_usd(weight_g)
    cost_var = (cost_usd + fwd) * cfg["fx"]
    cost_fix = cfg["safety"] + cfg["cs"]
    return cost_var, cost_fix


def margin_at(cost_usd, weight_g, sale_krw, cfg):
    cv, cf = _cost_parts(cost_usd, weight_g, cfg)
    return sale_krw - cv - sale_krw * cfg["fee"] - sale_krw * cfg["return_pct"] - cf


def price_for_pct(cost_usd, weight_g, margin_pct, cfg):
    cv, cf = _cost_parts(cost_usd, weight_g, cfg)
    denom = 1 - margin_pct - cfg["fee"] - cfg["return_pct"]
    return _round100((cv + cf) / denom) if denom > 0 else None


def price_for_abs(cost_usd, weight_g, M, cfg):
    cv, cf = _cost_parts(cost_usd, weight_g, cfg)
    denom = 1 - cfg["fee"] - cfg["return_pct"]
    return _round100((cv + cf + M) / denom)


def current_cost_usd(product, cfg):
    """할인가(landed) 우선 + flash 가드. 반환 (cost_usd, source)."""
    landed = product.get("landed_price_usd")
    amazon = product.get("amazon_price_usd") or product.get("cost_usd")
    amazon = float(amazon) if amazon else None
    if not landed or float(landed) <= 0:
        return amazon, ("amazon(정가)" if amazon else "none")
    landed = float(landed)
    # flash 가드: 최근 N일 snapshot 최대 landed 대비 너무 낮으면 일시할인 의심 → 보수적으로 정가
    try:
        with get_db() as c:
            r = c.execute(
                "SELECT MAX(landed_price_usd) m FROM amazon_price_snapshots "
                "WHERE asin=? AND fetched_at >= datetime('now', ?)",
                (product.get("asin"), f"-{cfg['flash_days']} days"),
            ).fetchone()
        recent_max = r["m"] if r and r["m"] else None
        if recent_max and landed < cfg["flash_ratio"] * float(recent_max):
            return (amazon or landed), "flash_guard(정가)"
    except Exception:
        pass
    return landed, "landed(할인가)"


def evaluate(product: dict, listing: dict, live_price=None, cfg=None) -> dict:
    cfg = cfg or load_config(listing.get("channel", "coupang"))
    cost, src = current_cost_usd(product, cfg)
    w = product.get("weight_g")
    mode = (listing.get("price_mode") or "auto").strip()
    gstate = (listing.get("guard_state") or "normal").strip()
    floor, deadband = cfg["floor"], cfg["deadband"]
    out = {"cost_usd": cost, "cost_src": src, "mode": mode, "guard_state": gstate,
           "target": None, "reason": "", "sanity_flag": False, "margin": None, "needs_put": False}
    if not cost or cost <= 0:
        out["reason"] = "원가없음(STALE)"; return out

    mtarget = listing.get("target_margin_override") or cfg["margin"]

    if mode == "auto":
        p_pct = price_for_pct(cost, w, mtarget, cfg) or 0
        p_floor = price_for_abs(cost, w, floor, cfg)
        out["target"] = max(p_pct, p_floor)
        out["guard_state"] = "normal"
        out["reason"] = f"auto {int(mtarget*100)}%↔floor{int(floor)} max"
    elif mode in ("manual_fixed", "manual_base"):
        if mode == "manual_base":
            base = listing.get("manual_price_krw") or 0
            disc = (product.get("discount_pct") or 0) / 100.0
            anchor = _round100(base * (1 - disc))
        else:
            anchor = int(listing.get("manual_price_krw") or 0)
        m_now = margin_at(cost, w, anchor, cfg)
        if gstate == "normal" and m_now <= 0:
            out["target"] = price_for_abs(cost, w, floor, cfg)
            out["guard_state"] = "raised"
            out["reason"] = f"손실가드 인상(마진{int(m_now)}≤0 → floor{int(floor)})"
        elif gstate == "raised" and m_now > deadband:
            out["target"] = anchor; out["guard_state"] = "normal"
            out["reason"] = f"회복(마진{int(m_now)}>{int(deadband)}) → 수동가 복귀"
        elif gstate == "raised":
            out["target"] = price_for_abs(cost, w, floor, cfg)
            out["reason"] = f"손실가드 유지(마진{int(m_now)}, raised)"
        else:
            out["target"] = anchor; out["reason"] = f"{mode} 수동가 유지"
    else:
        out["reason"] = f"unknown mode {mode}"; return out

    out["margin"] = int(margin_at(cost, w, out["target"], cfg))
    amazon_krw = (product.get("amazon_price_usd") or cost) * cfg["fx"]
    if amazon_krw and out["target"] > cfg["sanity_markup"] * amazon_krw:
        out["sanity_flag"] = True
        out["reason"] += f" ★sanity초과({out['target']/amazon_krw:.1f}>{cfg['sanity_markup']}배)"
    if live_price:
        out["needs_put"] = abs(out["target"] - int(live_price)) > cfg["put_threshold"]
    return out


def evaluate_product(product_id: int, channel: str = "coupang", live_price=None, cfg=None) -> dict:
    """DB에서 product+listing 읽어 평가."""
    with get_db() as c:
        p = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        l = c.execute("SELECT * FROM listings_pa WHERE product_id=? AND channel=?",
                      (product_id, channel)).fetchone()
    if not p:
        return {"reason": "product 없음", "target": None}
    return evaluate(dict(p), dict(l) if l else {"channel": channel}, live_price=live_price, cfg=cfg)


def initial_price(product: dict, channel: str = "coupang", cfg=None) -> dict:
    """등록 시 초기 가격 = auto 모드 평가 (할인가 cost basis + 새 수수료/반품 + floor + sanity).
    channel_listing_service / 등록 스크립트 호환 dict 반환."""
    cfg = cfg or load_config(channel)
    res = evaluate(product, {"channel": channel, "price_mode": "auto"}, cfg=cfg)
    cost = res.get("cost_usd")
    cv, _cf = _cost_parts(cost, product.get("weight_g"), cfg) if cost else (0, 0)
    return {
        "sale_krw": res["target"] or 0,
        "net_margin_krw": res["margin"] or 0,
        "cost_krw": int(cv),
        "fee_rate": cfg["fee"],
        "cost_src": res["cost_src"],
        "sanity_flag": res["sanity_flag"],
        "exchange_rate": cfg["fx"],
        "reason": res["reason"],
    }


def initial_price_by_id(product_id: int, channel: str = "coupang", cfg=None) -> dict:
    with get_db() as c:
        p = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        return {"sale_krw": 0, "net_margin_krw": 0, "cost_krw": 0, "fee_rate": 0,
                "cost_src": "none", "sanity_flag": False, "reason": "product 없음"}
    return initial_price(dict(p), channel=channel, cfg=cfg)
