"""coupang_winner.py — 쿠팡 위너 의심도 추적 시스템.

쿠팡 OPEN API 가 위너 정보를 직접 노출하지 않아, 주문 데이터 + 등록 경과일 기반
간접 추정. 100% 정확하지 않지만 비용 0, 차단 위험 0, 데이터 누적 시 정확도 향상.

판정 로직:
  - 등록 7일 미만           → too_new (판단 보류)
  - 최근 30일 주문 1건+      → winner_likely
  - 등록 30일+ 주문 0건      → suspect_loser
  - 그 외 (7~30일 주문 0건)  → unknown

운영 흐름:
  1. evaluate_all_listings() — 매일 또는 수동 트리거로 listings_pa 업데이트
  2. /api/pa/coupang/winner/non-winners — 비위너 의심 리스트
  3. /api/pa/coupang/winner/summary — 상태별 카운트
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(s[:19], fmt[:19])
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _classify(days_listed: int, order_count_30d: int) -> str:
    """위너 상태 분류."""
    if days_listed < 7:
        return "too_new"
    if order_count_30d >= 1:
        return "winner_likely"
    if days_listed >= 30:
        return "suspect_loser"
    return "unknown"


def evaluate_listing(listing_id: int) -> Optional[dict]:
    """단일 listing 의 위너 상태 갱신."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT l.id, l.product_id, l.channel_product_id, l.channel,
                      l.created_at, l.last_synced_at, l.status
               FROM listings_pa l
               WHERE l.id=? AND l.channel='coupang'""",
            (listing_id,),
        ).fetchone()
    if not row:
        return None

    listing_dt = _parse_dt(row["created_at"]) or _parse_dt(row["last_synced_at"])
    if not listing_dt:
        return None
    days_listed = (datetime.now(timezone.utc) - listing_dt).days

    # 최근 30일 주문 카운트
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    last_order_at = None
    order_count = 0
    try:
        with get_db() as conn:
            o = conn.execute(
                """SELECT COUNT(*) cnt, MAX(placed_at) last_at
                   FROM orders
                   WHERE channel='coupang' AND product_id=?
                     AND placed_at >= ?""",
                (row["product_id"], cutoff),
            ).fetchone()
            if o:
                order_count = o["cnt"] or 0
                last_order_at = o["last_at"]
    except Exception as e:
        logger.warning(f"[winner] order count 실패 listing={listing_id}: {e}")

    status = _classify(days_listed, order_count)

    with get_db() as conn:
        conn.execute(
            """UPDATE listings_pa
               SET winner_status=?, winner_checked_at=?,
                   last_order_at=?, days_listed=?, order_count_30d=?
               WHERE id=?""",
            (status, _now(), last_order_at, days_listed, order_count, listing_id),
        )

    return {
        "listing_id": listing_id,
        "winner_status": status,
        "days_listed": days_listed,
        "order_count_30d": order_count,
        "last_order_at": last_order_at,
    }


def evaluate_all_listings(channel: str = "coupang") -> dict:
    """전체 listed 상품 평가. 1885건 약 30초 (단순 SQL 합산)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id FROM listings_pa
               WHERE channel=? AND status='listed'""",
            (channel,),
        ).fetchall()

    total = len(rows)
    counts = {"too_new": 0, "winner_likely": 0, "suspect_loser": 0, "unknown": 0}
    errors = 0
    for r in rows:
        try:
            res = evaluate_listing(r["id"])
            if res:
                counts[res["winner_status"]] = counts.get(res["winner_status"], 0) + 1
        except Exception as e:
            errors += 1
            logger.warning(f"[winner] evaluate 실패 listing={r['id']}: {e}")

    logger.info(f"[winner] {channel} 평가 완료 — total={total} {counts} errors={errors}")
    return {"total": total, "counts": counts, "errors": errors}


def get_non_winners(channel: str = "coupang", limit: int = 100) -> list[dict]:
    """비위너 의심 (suspect_loser) 리스트 — 가격 정리/조정 후보."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT l.id as listing_id, l.product_id, l.channel_product_id,
                      l.sale_krw, l.days_listed, l.order_count_30d, l.last_order_at,
                      l.winner_status, l.winner_checked_at,
                      p.asin, p.title_ko, p.title_en, p.cost_usd
               FROM listings_pa l
               JOIN products p ON l.product_id=p.id
               WHERE l.channel=? AND l.status='listed'
                 AND l.winner_status='suspect_loser'
               ORDER BY l.days_listed DESC
               LIMIT ?""",
            (channel, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_summary(channel: str = "coupang") -> dict:
    """채널별 위너 상태 요약."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT COALESCE(winner_status, 'no_data') as status, COUNT(*) cnt
               FROM listings_pa
               WHERE channel=? AND status='listed'
               GROUP BY winner_status""",
            (channel,),
        ).fetchall()
    return {r["status"]: r["cnt"] for r in rows}
