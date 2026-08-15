"""sid 20 재promote — stale 정리 후 남은 sourcing_candidates(sid 20 3,334)를 promote.
run_promote_background는 candidates 전체를 처리하므로, clean_stale_candidates 선행 필수.
products 0건 상태에서 재실행 → 중복 없음. 한국mfr/safety/dedup 게이트 자동 작동.
조용한 창(데일리 DB쓰기 타이머 회피)에서 실행할 것."""
import asyncio
import logging
import os
import sys
from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend.purchase.database import get_db
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("repromote-sid20")


async def main():
    from backend.purchase.services.sourcing_promote import (
        create_promote_job, run_promote_background, get_running_promote_job)
    running = get_running_promote_job()
    if running:
        logger.warning(f"이미 실행 중인 promote job: {running['id']} — 중단"); return
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM sourcing_candidates").fetchone()["c"]
    logger.info(f"promote 대상 candidates: {total}건")
    if total == 0:
        logger.info("candidates 0 — 종료"); return
    if total > 4000:
        logger.warning(f"★candidates {total} > 4000 — stale 미정리 의심. 중단 (clean_stale 먼저)"); return
    job_id = create_promote_job(total)
    logger.info(f"promote job 시작: {job_id}")
    await run_promote_background(job_id)
    with get_db() as conn:
        j = conn.execute("SELECT status, total FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
        prod = conn.execute("SELECT COUNT(*) FROM products WHERE created_at>='2026-05-25' AND status='draft'").fetchone()[0]
    logger.info(f"promote 완료: job status={j['status'] if j else '?'} | 05-25 draft products={prod}")
    logger.info("=== 완료 ===")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        logger.exception("=== 예외 ===")
        sys.exit(1)
