"""coupang_status_sync.py — 쿠팡 셀러상품 status 동기화.

쿠팡 OPEN API GET /seller-products/{id} 응답의 statusName/status 를
listings_pa 에 저장. 반려(REJECTED) 상품 식별용.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from backend.purchase.database import get_db
from backend.purchase.services.coupang_service import get_seller_product

logger = logging.getLogger(__name__)

_RATE_LIMIT_SEC = 0.55  # 쿠팡 OPEN API rate limit 보호


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def sync_one(channel_product_id: str) -> Optional[dict]:
    """단일 셀러 상품 status 조회 + DB 업데이트."""
    body = get_seller_product(channel_product_id)
    if not body or not body.get("data"):
        return None
    data = body["data"]
    status = data.get("status")
    status_name = data.get("statusName")

    with get_db() as conn:
        conn.execute(
            """UPDATE listings_pa
               SET coupang_seller_status=?, coupang_status_name=?,
                   coupang_status_synced_at=?
               WHERE channel='coupang' AND channel_product_id=?""",
            (status, status_name, _now(), channel_product_id),
        )
    return {"channel_product_id": channel_product_id, "status": status, "statusName": status_name}


def sync_all(only_unchecked_days: int | None = None,
             only_status: str = "listed") -> dict:
    """전체 listed 쿠팡 상품 status 동기화.

    Args:
        only_unchecked_days: 마지막 sync 후 N일 이상 경과한 것만 갱신 (None=전체)
        only_status: 우리 DB status 필터 (기본 'listed')
    """
    where = "WHERE channel='coupang' AND status=? AND channel_product_id IS NOT NULL"
    params: list = [only_status]
    if only_unchecked_days is not None:
        where += " AND (coupang_status_synced_at IS NULL OR coupang_status_synced_at < datetime('now', ?))"
        params.append(f"-{only_unchecked_days} days")

    with get_db() as conn:
        rows = conn.execute(
            f"SELECT channel_product_id FROM listings_pa {where}",
            params,
        ).fetchall()

    total = len(rows)
    counts: dict = {}
    errors = 0
    logger.info(f"[status-sync] 대상 {total}건")

    for i, r in enumerate(rows, 1):
        cpid = r["channel_product_id"]
        try:
            res = sync_one(cpid)
            if res:
                key = res["status"] or "UNKNOWN"
                counts[key] = counts.get(key, 0) + 1
            else:
                errors += 1
        except Exception as e:
            logger.warning(f"[status-sync] cpid={cpid}: {e}")
            errors += 1
        if i % 100 == 0:
            logger.info(f"[status-sync] 진행 {i}/{total} counts={counts} errors={errors}")
        time.sleep(_RATE_LIMIT_SEC)

    logger.info(f"[status-sync] 완료 — total={total} counts={counts} errors={errors}")
    return {"total": total, "counts": counts, "errors": errors}


def get_rejected(limit: int = 200) -> list[dict]:
    """반려/거래정지 상품 리스트."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT l.id, l.product_id, l.channel_product_id, l.sale_krw,
                      l.coupang_seller_status, l.coupang_status_name,
                      l.coupang_status_synced_at,
                      p.asin, p.title_ko, p.title_en
               FROM listings_pa l
               JOIN products p ON l.product_id=p.id
               WHERE l.channel='coupang'
                 AND l.coupang_seller_status IN ('REJECTED', 'BLOCKED', 'PARTIAL_APPROVED')
               ORDER BY l.coupang_status_synced_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_status_summary() -> dict:
    """쿠팡 status 별 카운트."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT COALESCE(coupang_seller_status, 'UNSYNCED') as status,
                      COALESCE(coupang_status_name, '미동기화') as status_name,
                      COUNT(*) cnt
               FROM listings_pa
               WHERE channel='coupang' AND status='listed'
               GROUP BY coupang_seller_status, coupang_status_name
               ORDER BY cnt DESC"""
        ).fetchall()
    return {r["status_name"]: {"status": r["status"], "count": r["cnt"]} for r in rows}
