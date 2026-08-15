"""sid=10 전용 catch-up — 5,116 잔여 detail → listing → upload.

product 47514 단일 테스트 성공 확인 후 (10:21 listed, spid 16202742280) 본격 진행.

전제: streaming 사고로 detail_pages 일부 INSERT 됐을 수 있음 → DELETE 로 force 재처리.

실행:
  nohup python -m backend.purchase.scripts.catchup_sid10 \\
      > /tmp/catchup_sid10.log 2>&1 &

5K 한도 도달 시 lister 가 자체 pending 마킹 → 다음날 quota_retry 가 catch-up.
"""
import argparse
import asyncio
import logging
import os
import sys
import uuid

from dotenv import load_dotenv
_ROOT = os.environ.get(
    "CHARISG_ROOT",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
)
load_dotenv(os.path.join(_ROOT, ".env"))

from backend.purchase import database
from backend.purchase.database import get_db
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("catchup-sid10")

# sid=10 sourcing_id range (이전 분석에서 확인)
SID10_PID_MIN = 47514
SID10_PID_MAX = 52630


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def main() -> None:
    logger.info(f"=== sid=10 catch-up 시작 — pid range {SID10_PID_MIN}~{SID10_PID_MAX} ===")

    # 0. 대상 SELECT (ai_processed_at IS NULL + cost_usd 정상)
    with get_db() as conn:
        pids = [r["id"] for r in conn.execute(
            f"""SELECT id FROM products
                WHERE id BETWEEN {SID10_PID_MIN} AND {SID10_PID_MAX}
                  AND ai_processed_at IS NULL
                  AND cost_usd > 0
                ORDER BY id"""
        ).fetchall()]
    logger.info(f"[0] 대상 (cost_usd ok + ai_processed_at NULL): {len(pids)}건")
    if not pids:
        logger.info("대상 0건 — 종료")
        return

    # 1. detail_pages 정리 (streaming 사고 잔재 — Stage 1 skip 우회)
    with get_db() as conn:
        ph = ",".join("?" * len(pids))
        cur = conn.execute(f"DELETE FROM detail_pages WHERE product_id IN ({ph})", pids)
    logger.info(f"[1] detail_pages 정리: {cur.rowcount}건 DELETE")

    # 2. detail (run_two_stage_batch)
    from backend.purchase.services.ai_processor import run_two_stage_batch
    job1 = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO batch_jobs (id, job_type, status, total, created_at) "
            "VALUES (?, 'ai_detail', 'pending', ?, ?)",
            (job1, len(pids), _now_iso()),
        )
    logger.info(f"[2] detail 시작 — job={job1} count={len(pids)}")
    await run_two_stage_batch(job1, pids)
    with get_db() as conn:
        ai_done = conn.execute(
            f"SELECT COUNT(*) FROM products WHERE id IN ({ph}) AND ai_processed_at IS NOT NULL",
            pids,
        ).fetchone()[0]
    logger.info(f"[2] detail 완료 — ai_processed_at 채워진 {ai_done}/{len(pids)}건")

    # 3. channel listing (send_to_channels)
    from backend.purchase.services.channel_listing_service import send_to_channels
    listed_eligible = [p for p in pids[:ai_done]]  # ai_processed_at 채워진 분만
    # 정확히 다시 SELECT
    with get_db() as conn:
        ready_pids = [r["id"] for r in conn.execute(
            f"""SELECT p.id FROM products p
                WHERE p.id IN ({ph}) AND p.ai_processed_at IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM listings_pa l
                     WHERE l.product_id=p.id AND l.channel='coupang'
                  )""",
            pids,
        ).fetchall()]
    logger.info(f"[3] channel listing 시작 — {len(ready_pids)}건")
    sent = 0
    failed = 0
    for i, pid in enumerate(ready_pids, 1):
        try:
            await asyncio.to_thread(send_to_channels, pid, ["coupang"])
            sent += 1
        except Exception as e:
            failed += 1
            if failed <= 10:
                logger.warning(f"[3] pid={pid} fail: {e}")
        if i % 100 == 0:
            logger.info(f"[3] {i}/{len(ready_pids)} — sent={sent} failed={failed}")
    logger.info(f"[3] channel listing 완료 — sent={sent} failed={failed}")

    # 4. coupang upload (5K 한도 자동 적용)
    from backend.purchase.routers.coupang import _run_coupang_upload_bg
    with get_db() as conn:
        cu_pids = [r["product_id"] for r in conn.execute(
            f"""SELECT DISTINCT product_id FROM listings_pa
                WHERE channel='coupang' AND status='pending'
                  AND channel_product_id IS NULL
                  AND product_id IN ({ph})""",
            pids,
        ).fetchall()]
    if not cu_pids:
        logger.info("[4] coupang pending 0건 — skip")
        return
    job2 = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO batch_jobs (id, job_type, status, total, created_at) "
            "VALUES (?, 'coupang_upload', 'pending', ?, ?)",
            (job2, len(cu_pids), _now_iso()),
        )
    logger.info(f"[4] coupang upload 시작 — job={job2} count={len(cu_pids)}")
    await _run_coupang_upload_bg(job2, cu_pids)
    with get_db() as conn:
        ph2 = ",".join("?" * len(cu_pids))
        listed_cnt = conn.execute(
            f"""SELECT COUNT(*) FROM listings_pa
                WHERE channel='coupang' AND status='listed'
                  AND product_id IN ({ph2})""",
            cu_pids,
        ).fetchone()[0]
        pending_cnt = conn.execute(
            f"""SELECT COUNT(*) FROM listings_pa
                WHERE channel='coupang' AND status='pending'
                  AND product_id IN ({ph2})""",
            cu_pids,
        ).fetchone()[0]
    logger.info(f"[4] coupang upload 완료 — listed={listed_cnt} pending={pending_cnt} (5K 한도 시 pending → 내일 quota_retry)")
    logger.info("=== catch-up 완료 ===")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        logger.exception("=== catch-up 예외 ===")
        sys.exit(1)
