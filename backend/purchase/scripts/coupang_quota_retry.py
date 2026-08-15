"""쿠팡 5,000/일 한도 잔여 listings 자동 재시도 (본업: pending 재업로드만).

매일 KST 00:05 (UTC 15:05) systemd timer 로 1회 실행 — 자정 한도리셋 직후.
1) listings_pa.status='pending' AND channel='coupang' 인 행 _run_coupang_upload_bg 재처리

★KR 직배 검증 + forwarder 재산정은 전담 잡 verify-unchecked(매일 17:00 UTC)로 분리.
  (2026-06-04: 기존 step2 run_batch_verify(coupang_chunk=3000) + step3 forwarder 가
   verify-unchecked 와 완전 중복 + amazon.com 스크래핑 2h 동안 turnstile 독점락 호그
   → 정기잡 herd 차단 유발. step4 쿠폰적용이 coupon-catchup 으로 분리됐던 것과 동일 사유로 제거.)
  quota-retry 가 만든 신규 listed 는 kr_shipping_eligible 미설정 상태로 남고,
  verify-unchecked 가 unchecked 대상으로 다음 17:00 UTC 슬롯에 흡수 검증.

정착 위치: backend/purchase/scripts/coupang_quota_retry.py
실행: python -m backend.purchase.scripts.coupang_quota_retry
"""
import asyncio
import logging
import uuid

from backend.purchase.database import get_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("cp-quota-retry")


async def main() -> None:
    from backend.purchase.routers.coupang import _run_coupang_upload_bg

    # ── 쿠팡 pending 재처리 (어제 한도에 막힌 신규등록 재시도) ──
    with get_db() as conn:
        rows = conn.execute(
            "SELECT product_id FROM listings_pa "
            "WHERE channel='coupang' AND status='pending'"
        ).fetchall()
    cu_pids = [r["product_id"] for r in rows]
    logger.info(f"쿠팡 quota retry 대상: {len(cu_pids)}건")

    if cu_pids:
        job_id = uuid.uuid4().hex[:12]
        with get_db() as conn:
            conn.execute(
                "INSERT INTO batch_jobs (id, job_type, status, total, created_at) "
                "VALUES (?, 'coupang_upload', 'pending', ?, datetime('now')) ",
                (job_id, len(cu_pids)),
            )
        logger.info(f"coupang_upload retry job_id={job_id}")
        await _run_coupang_upload_bg(job_id, cu_pids)
        logger.info("=== 쿠팡 quota retry 완료 ===")
    else:
        logger.info("쿠팡 pending 0건 — upload 단계 skip")

    logger.info("=== 전체 완료 (KR검증/forwarder=verify-unchecked, 쿠폰=coupon-catchup 로 분리) ===")


if __name__ == "__main__":
    asyncio.run(main())
