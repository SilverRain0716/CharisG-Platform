"""단일 product 의 전체 파이프라인 테스트 — detail → listing → coupang upload.

용도: sid=10 catch-up 전 1건 검증. 실패 패턴 파악 후 본격 진행.

실행:
  python -m backend.purchase.scripts.test_single_pipeline <product_id>
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

# PA API 의 lifespan 에서 호출되는 register_db_factory 를 별도 process 에서 호출
register_db_factory(database.get_db)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("test-single")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def main(pid: int) -> None:
    logger.info(f"=== single product 테스트 — pid={pid} ===")

    # 시작 상태
    with get_db() as conn:
        p = conn.execute("SELECT id, asin, cost_usd, ai_processed_at, status FROM products WHERE id=?", (pid,)).fetchone()
    logger.info(f"[before] asin={p['asin']} cost={p['cost_usd']} ai={p['ai_processed_at']} status={p['status']}")

    # Step 1: detail
    logger.info("[step1] detail (run_two_stage_batch) 시작")
    from backend.purchase.services.ai_processor import run_two_stage_batch
    job1 = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO batch_jobs (id, job_type, status, total, created_at) "
            "VALUES (?, 'ai_detail', 'pending', 1, ?)",
            (job1, _now_iso()),
        )
    await run_two_stage_batch(job1, [pid])
    with get_db() as conn:
        p = conn.execute("SELECT ai_processed_at FROM products WHERE id=?", (pid,)).fetchone()
    logger.info(f"[step1] done — ai_processed_at={p['ai_processed_at']}")
    if not p["ai_processed_at"]:
        logger.error("[step1] FAIL — ai_processed_at 미설정, 중단")
        return

    # Step 2: channel listing
    logger.info("[step2] send_to_channels(coupang) 시작")
    try:
        from backend.purchase.services.channel_listing_service import send_to_channels
        result = await asyncio.to_thread(send_to_channels, pid, ["coupang"])
        logger.info(f"[step2] done — {result}")
    except Exception as e:
        logger.exception(f"[step2] FAIL — {e}")
        return
    with get_db() as conn:
        lp = conn.execute(
            "SELECT status, channel_product_id, error_message FROM listings_pa "
            "WHERE product_id=? AND channel='coupang'",
            (pid,),
        ).fetchone()
    logger.info(f"[step2] listings_pa: status={lp['status']} spid={lp['channel_product_id']} err={lp['error_message']}")

    # Step 3: coupang upload (lister 호출)
    logger.info("[step3] _run_coupang_upload_bg 시작")
    from backend.purchase.routers.coupang import _run_coupang_upload_bg
    job2 = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO batch_jobs (id, job_type, status, total, created_at) "
            "VALUES (?, 'coupang_upload', 'pending', 1, ?)",
            (job2, _now_iso()),
        )
    await _run_coupang_upload_bg(job2, [pid])
    with get_db() as conn:
        lp = conn.execute(
            "SELECT status, channel_product_id, error_message FROM listings_pa "
            "WHERE product_id=? AND channel='coupang'",
            (pid,),
        ).fetchone()
    logger.info(f"[step3] done — status={lp['status']} spid={lp['channel_product_id']} err={lp['error_message']}")

    if lp["status"] == "listed":
        logger.info(f"=== ✅ 성공 — pid={pid} 쿠팡 listed (spid={lp['channel_product_id']}) ===")
    else:
        logger.warning(f"=== ⚠️ 부분 실패 — pid={pid} status={lp['status']} ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pid", type=int)
    args = ap.parse_args()
    try:
        asyncio.run(main(args.pid))
    except Exception:
        logger.exception("=== 예외 ===")
        sys.exit(1)
