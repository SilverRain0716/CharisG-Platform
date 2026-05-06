"""직배 불가(kr_shipping_eligible=0) 상품의 배대지 경유 가격 재산정 + 자동 분류.

인상률 기준 3-tier 자동 정책:
  < 15%       → 'reprice'        (sale_krw 자동 변경, 마진 35% 보존)
  15% ~ 30%   → 'margin_shrink'  (가격 유지, 실질 마진 재계산)
  > 30%       → 'mark_exclude'   (마킹만, 실제 채널 비활성은 별도 endpoint)

채널 동기화(스마트스토어/쿠팡 실제 가격 변경)는 자동 X — 사용자가 별도 트리거.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from backend.purchase.database import get_db
from backend.purchase.services.forwarder_shipping import forwarder_shipping_usd
from backend.purchase.services.pricing_service_pa import calculate_sale_krw

logger = logging.getLogger(__name__)

# 3-tier 임계값 (인상률 %)
TIER_REPRICE_MAX = 15.0       # 0~15% : 가격 인상
TIER_MARGIN_SHRINK_MAX = 30.0 # 15~30% : 마진 축소 (가격 유지)
                              # > 30% : excluded 후보 마킹


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decide_action(uplift_pct: float) -> str:
    if uplift_pct < TIER_REPRICE_MAX:
        return "reprice"
    if uplift_pct < TIER_MARGIN_SHRINK_MAX:
        return "margin_shrink"
    return "mark_exclude"


def recalculate_blocked_listings(
    apply: bool = True,
    channel: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """kr_shipping_eligible=0 인 listings_pa 를 일괄 재산정 + 자동 분류.

    Args:
        apply: True 면 DB 업데이트, False 면 dry-run (분류 결과만 반환).
        channel: 'smartstore' | 'coupang' | None.
        limit: 처리 건수 제한 (None = 전체).
    Returns:
        {processed, by_action: {...}, samples: [...]}
    """
    sql = """SELECT lp.id, lp.channel, lp.sale_krw, lp.cost_krw_snapshot,
                    p.cost_usd, p.weight_g, p.asin
               FROM listings_pa lp
               JOIN products p ON p.id = lp.product_id
              WHERE lp.status='listed'
                AND lp.kr_shipping_eligible = 0
                AND p.cost_usd IS NOT NULL"""
    params: list = []
    if channel:
        sql += " AND lp.channel = ?"
        params.append(channel)
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    by_action = {"reprice": 0, "margin_shrink": 0, "mark_exclude": 0, "keep": 0}
    samples: list[dict] = []
    processed = 0
    now = _now_iso()

    with get_db() as conn:
        for r in rows:
            try:
                cost_usd = r["cost_usd"]
                cur_price = r["sale_krw"] or 0
                fw_usd = forwarder_shipping_usd(r["weight_g"])

                new = calculate_sale_krw(
                    cost_usd=cost_usd,
                    cj_shipping_usd=fw_usd,
                    channel=r["channel"],
                )
                required_price = new["sale_krw"]

                if cur_price <= 0:
                    action = "keep"
                    uplift_pct = 0.0
                else:
                    uplift_pct = (required_price - cur_price) / cur_price * 100.0
                    if uplift_pct <= 0:
                        action = "keep"  # 인상 불필요
                    else:
                        action = _decide_action(uplift_pct)

                # 적용
                if apply:
                    if action == "reprice":
                        conn.execute(
                            """UPDATE listings_pa
                                  SET sale_krw = ?,
                                      forwarder_shipping_usd = ?,
                                      forwarder_required_price_krw = ?,
                                      forwarder_action = ?,
                                      forwarder_processed_at = ?
                                WHERE id = ?""",
                            (required_price, fw_usd, required_price, action, now, r["id"]),
                        )
                    else:
                        # margin_shrink / mark_exclude / keep — sale_krw 유지, 메타만 갱신
                        conn.execute(
                            """UPDATE listings_pa
                                  SET forwarder_shipping_usd = ?,
                                      forwarder_required_price_krw = ?,
                                      forwarder_action = ?,
                                      forwarder_processed_at = ?
                                WHERE id = ?""",
                            (fw_usd, required_price, action, now, r["id"]),
                        )

                by_action[action] = by_action.get(action, 0) + 1
                processed += 1

                if len(samples) < 10:
                    samples.append({
                        "id": r["id"],
                        "channel": r["channel"],
                        "asin": r["asin"],
                        "weight_g": r["weight_g"],
                        "cost_usd": cost_usd,
                        "fw_usd": fw_usd,
                        "current_price": cur_price,
                        "required_price": required_price,
                        "uplift_pct": round(uplift_pct, 1),
                        "action": action,
                    })
            except Exception as e:
                logger.warning("[forwarder-pricing] %s 실패: %s", r["asin"], e)

    return {
        "processed": processed,
        "applied": apply,
        "by_action": by_action,
        "tier_thresholds": {
            "reprice_max_pct": TIER_REPRICE_MAX,
            "margin_shrink_max_pct": TIER_MARGIN_SHRINK_MAX,
        },
        "samples": samples,
    }


def summary() -> dict:
    """forwarder 분류 결과 통계."""
    with get_db() as conn:
        overall = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN forwarder_action IS NULL THEN 1 ELSE 0 END) AS unprocessed,
                      SUM(CASE WHEN forwarder_action='reprice' THEN 1 ELSE 0 END) AS reprice,
                      SUM(CASE WHEN forwarder_action='margin_shrink' THEN 1 ELSE 0 END) AS margin_shrink,
                      SUM(CASE WHEN forwarder_action='mark_exclude' THEN 1 ELSE 0 END) AS mark_exclude,
                      SUM(CASE WHEN forwarder_action='keep' THEN 1 ELSE 0 END) AS keep
                 FROM listings_pa
                WHERE status='listed' AND kr_shipping_eligible=0"""
        ).fetchone()
        per_ch = conn.execute(
            """SELECT channel,
                      COUNT(*) AS total,
                      SUM(CASE WHEN forwarder_action='reprice' THEN 1 ELSE 0 END) AS reprice,
                      SUM(CASE WHEN forwarder_action='margin_shrink' THEN 1 ELSE 0 END) AS margin_shrink,
                      SUM(CASE WHEN forwarder_action='mark_exclude' THEN 1 ELSE 0 END) AS mark_exclude,
                      SUM(CASE WHEN forwarder_action='keep' THEN 1 ELSE 0 END) AS keep
                 FROM listings_pa
                WHERE status='listed' AND kr_shipping_eligible=0
                GROUP BY channel"""
        ).fetchall()
    return {
        "overall": dict(overall) if overall else {},
        "per_channel": [dict(r) for r in per_ch],
    }


def apply_exclusions(channel: Optional[str] = None, limit: Optional[int] = None) -> dict:
    """forwarder_action='mark_exclude' 인 listings 의 status='excluded' 변경.

    이건 destructive — 채널 검색에서 사라짐. 사용자가 명시적으로 트리거.
    """
    sql = """UPDATE listings_pa
                SET status = 'excluded'
              WHERE status = 'listed'
                AND forwarder_action = 'mark_exclude'"""
    params: list = []
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    if limit:
        sql += f" AND id IN (SELECT id FROM listings_pa WHERE status='listed' AND forwarder_action='mark_exclude'"
        if channel:
            sql += " AND channel = ?"
            params.append(channel)
        sql += " LIMIT ?)"
        params.append(limit)
    with get_db() as conn:
        cur = conn.execute(sql, params)
        affected = cur.rowcount
    return {"affected": affected, "channel": channel, "limit": limit}
