"""쿠팡 임시저장 상품 일괄 승인 요청 — 백그라운드 job.

대상:
   listings_pa WHERE channel='coupang'
     AND status='listed'
     AND channel_product_id IS NOT NULL AND channel_product_id != ''
     AND approval_requested_at IS NULL

각 sellerProductId 에 대해 coupang_service.request_approval() 호출.
성공 시 listings_pa.approval_requested_at = now 로 기록 → 재실행 시 중복 호출 방지.

실패 시 error_message 에 누적 기록 (덮어씀). status 는 유지 (listed) — 승인 요청 실패는
상품 자체의 등록 실패가 아니므로.

batch_jobs 재사용, job_type='coupang_approval'.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from backend.purchase.database import get_db
from backend.purchase.services.coupang_service import request_approval

logger = logging.getLogger(__name__)

JOB_TYPE = "coupang_approval"

# 쿠팡 API rate limit 보호 (과거 업로드 작업 기준 5 req/s 수준은 문제 없었음)
_INTERVAL = 0.3


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def count_pending_approval() -> int:
    """승인 요청 대기 중인 listed 건수."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) c FROM listings_pa
               WHERE channel='coupang'
                 AND status='listed'
                 AND channel_product_id IS NOT NULL AND channel_product_id != ''
                 AND approval_requested_at IS NULL"""
        ).fetchone()
    return row["c"] if row else 0


def create_job(total: int) -> str:
    job_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            """INSERT INTO batch_jobs (id, job_type, status, total, created_at, phase_message)
               VALUES (?, ?, 'pending', ?, ?, ?)""",
            (job_id, JOB_TYPE, total, _now_iso(), "대기 중"),
        )
    return job_id


def get_job(job_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM batch_jobs WHERE id=? AND job_type=?",
            (job_id, JOB_TYPE),
        ).fetchone()
    return dict(row) if row else None


def get_running_job() -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM batch_jobs
               WHERE job_type=? AND status IN ('pending','running')
               ORDER BY created_at DESC LIMIT 1""",
            (JOB_TYPE,),
        ).fetchone()
    return dict(row) if row else None


async def run_approval_background(job_id: str) -> None:
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET status='running', started_at=?, phase_message=? WHERE id=?",
                (_now_iso(), "대상 목록 조회 중", job_id),
            )
            rows = conn.execute(
                """SELECT id, product_id, channel_product_id FROM listings_pa
                   WHERE channel='coupang'
                     AND status='listed'
                     AND channel_product_id IS NOT NULL AND channel_product_id != ''
                     AND approval_requested_at IS NULL
                   ORDER BY product_id"""
            ).fetchall()
        rows = [dict(r) for r in rows]

        total = len(rows)
        if total == 0:
            with get_db() as conn:
                conn.execute(
                    """UPDATE batch_jobs
                       SET status='done', total=0, finished_at=?, phase_message='승인 요청 대상 없음'
                       WHERE id=?""",
                    (_now_iso(), job_id),
                )
            return

        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET total=?, phase_message=? WHERE id=?",
                (total, f"승인 요청 중 0/{total}", job_id),
            )

        processed = 0
        errors = 0
        for idx, r in enumerate(rows, start=1):
            spid = r["channel_product_id"]
            try:
                ok, err = await asyncio.to_thread(request_approval, spid)
            except Exception as e:
                ok, err = False, f"예외: {e}"
                logger.exception(f"[coupang-approval {job_id}] product {r['product_id']} 예외")

            now = _now_iso()
            if ok:
                with get_db() as conn:
                    conn.execute(
                        """UPDATE listings_pa
                           SET approval_requested_at=?, last_synced_at=CURRENT_TIMESTAMP,
                               error_message=NULL
                           WHERE id=?""",
                        (now, r["id"]),
                    )
            else:
                errors += 1
                with get_db() as conn:
                    conn.execute(
                        """UPDATE listings_pa
                           SET error_message=?, last_synced_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (f"승인 요청 실패: {err}", r["id"]),
                    )
                logger.warning(f"[coupang-approval {job_id}] product {r['product_id']} (spid={spid}) 실패: {err}")

            processed = idx
            with get_db() as conn:
                conn.execute(
                    "UPDATE batch_jobs SET processed=?, errors=?, phase_message=? WHERE id=?",
                    (processed, errors, f"승인 요청 중 {processed}/{total}", job_id),
                )
            if idx < total:
                await asyncio.sleep(_INTERVAL)

        ok_count = processed - errors
        with get_db() as conn:
            conn.execute(
                """UPDATE batch_jobs
                   SET status='done', finished_at=?, phase_message=?
                   WHERE id=?""",
                (_now_iso(), f"완료 — 승인 요청 성공 {ok_count}, 실패 {errors} / {total}", job_id),
            )
        logger.info(f"[coupang-approval {job_id}] 완료 — 성공 {ok_count}, 실패 {errors}")

    except Exception as e:
        logger.exception(f"[coupang-approval {job_id}] 실패")
        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET status='error', error_message=?, finished_at=? WHERE id=?",
                (str(e), _now_iso(), job_id),
            )
