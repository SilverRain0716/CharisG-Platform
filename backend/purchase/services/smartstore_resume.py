"""Smartstore 잔여 pending 재개 — sheet_queue cancel/한도 초과 후 한도 정리 시 호출.

흐름:
  1. listings_pa.channel='smartstore' AND status='pending' 모두 수집
  2. listing_rotation.calculate_swap_needed → swap (네이버 한도 여유 부족 시 자동)
  3. _run_upload_background (D안 카테고리 conveyor)

쿠팡은 건드리지 않음.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def resume_smartstore_pending() -> dict:
    """모든 status='pending' smartstore listings 를 swap+register 진행."""
    with get_db() as conn:
        ss_pids = [r[0] for r in conn.execute(
            "SELECT product_id FROM listings_pa "
            "WHERE channel='smartstore' AND status='pending'"
        ).fetchall()]
    if not ss_pids:
        logger.info("[resume] pending smartstore 없음 — skip")
        return {"pids": 0, "job_id": None, "swap": None}

    logger.info(f"[resume] pending smartstore {len(ss_pids)}건 재개 시작")

    # 1) 한도 회전 (자동)
    swap_result = None
    try:
        from backend.purchase.services.listing_rotation import (
            calculate_swap_needed, swap_oldest_no_sales,
        )
        from backend.purchase.services.notifier import notify_swap_complete
        needed = calculate_swap_needed(len(ss_pids))
        if needed > 0:
            logger.info(f"[resume] 한도 회전 — {needed}건 영구삭제 swap")
            swap_result = await swap_oldest_no_sales(needed)
            try:
                notify_swap_complete(
                    swap_result["requested"], swap_result["ok"], swap_result["fail"],
                )
            except Exception as e:
                logger.warning(f"[resume] swap 알림 실패 (무시): {e}")
    except Exception as e:
        logger.exception(f"[resume] swap 실패 (계속 진행): {e}")

    # 2) register (D안 카테고리 conveyor)
    from backend.purchase.routers.smartstore import _run_upload_background

    job_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            """INSERT INTO batch_jobs (id, job_type, status, total, created_at)
               VALUES (?, 'smartstore_upload', 'pending', ?, ?)""",
            (job_id, len(ss_pids), _now()),
        )

    # background 실행 — caller 는 즉시 반환받고 진척은 batch_jobs/_run 에서 추적
    asyncio.create_task(_run_upload_background(job_id, ss_pids, "smartstore"))

    return {
        "pids": len(ss_pids),
        "job_id": job_id,
        "swap": swap_result,
    }
