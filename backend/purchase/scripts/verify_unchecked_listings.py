"""
verify_unchecked_listings.py — 미검증 listings 자동 일괄 처리 (daily timer).

신규 group 등록(register_new_group_listing) / Retrofit / 수동 등록 등으로 listings_pa 에
새로 들어왔지만 `kr_shipping_eligible IS NULL` 인 행을 일괄 검증 → `kr_shipping_eligible=0`
판정된 행은 forwarder 가격 재산정(recalculate_blocked_listings)으로 정정.

sheet_queue_worker 가 7,8 단계에서 inline 으로 호출하던 것을 timer 화 → 신규/Retrofit 흐름이
자동으로 사후 처리되도록 보장. 양산 시 부하 분산을 위해 매일 1회만 실행 (run_batch_verify 가
idempotent + chunk 반복이라 미검증분 0 될 때까지 처리).

실행:
    cd /home/ubuntu/CharisG-Platform/charisg-platform
    .venv/bin/python -m backend.purchase.scripts.verify_unchecked_listings
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone

from backend.purchase.database import get_db

logger = logging.getLogger("verify-unchecked")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _count_unchecked() -> tuple[int, int]:
    """(KR 미검증 수, forwarder 미처리 수) 반환."""
    with get_db() as conn:
        kr = conn.execute(
            """SELECT COUNT(*) c FROM listings_pa
               WHERE status IN ('pending','listed') AND kr_shipping_eligible IS NULL"""
        ).fetchone()["c"]
        fw = conn.execute(
            """SELECT COUNT(*) c FROM listings_pa
               WHERE status IN ('pending','listed') AND kr_shipping_eligible=0
                 AND forwarder_action IS NULL"""
        ).fetchone()["c"]
    return kr, fw


async def _run_kr_verify() -> str:
    """run_batch_verify 호출 (batch_jobs row 는 run_batch_verify 가 _job_init 으로 생성)."""
    from backend.purchase.services.kr_shipping_verifier import run_batch_verify
    job_id = uuid.uuid4().hex[:12]
    # 2026-06-02 버그수정: 사전 INSERT 제거 — run_batch_verify 가 내부 _job_init 으로
    # batch_jobs INSERT 하므로, 여기서 미리 INSERT 하면 매번 UNIQUE constraint failed:
    # batch_jobs.id 로 서비스 실패(05-29~ kr_verify pending 적체 원인). job_id 만 전달.
    await run_batch_verify(job_id)
    return job_id


def main() -> int:
    kr_before, fw_before = _count_unchecked()
    logger.info(f"시작: KR 미검증 {kr_before}건, forwarder 미처리 {fw_before}건")

    # 1) KR 검증 (있을 때만)
    if kr_before > 0:
        try:
            job_id = asyncio.run(_run_kr_verify())
            logger.info(f"KR 검증 batch_jobs id={job_id} 완료")
        except Exception as e:
            logger.exception(f"KR 검증 실패: {e}")
            return 1
    else:
        logger.info("KR 미검증 0건 — 검증 스킵")

    # 2) forwarder 가격 재산정 (KR 검증 후 새로 0 으로 판정된 분 포함)
    try:
        from backend.purchase.services.forwarder_pricing import recalculate_blocked_listings
        result = recalculate_blocked_listings(apply=True, channel=None, limit=None)
        logger.info(f"forwarder 재산정 결과: {json.dumps(result, ensure_ascii=False, default=str)[:600]}")
    except Exception as e:
        logger.exception(f"forwarder 재산정 실패: {e}")
        return 1

    kr_after, fw_after = _count_unchecked()
    logger.info(f"완료: KR 미검증 {kr_before}→{kr_after}, forwarder 미처리 {fw_before}→{fw_after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
