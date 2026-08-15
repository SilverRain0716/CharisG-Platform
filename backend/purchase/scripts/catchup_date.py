"""날짜 범위 catch-up — created_at in [from,to) 인 draft products 를 AI→카테고리→쿠팡 리스팅.
catchup_sid18 일반화. sid 20 재promote 후 (created 2026-05-25) 처리용.
이미 listed 된 건(11번가分)은 [2]/[3] 의 'coupang listing 없음' 필터로 자동 skip.

실행:
  nohup .venv/bin/python -m backend.purchase.scripts.catchup_date --from 2026-05-25 --to 2026-05-26 > /tmp/catchup_date.log 2>&1 &
"""
import argparse
import asyncio
import logging
import os
import sys
import uuid

from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend.purchase.database import get_db
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)
AI_CHUNK = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("catchup-date")


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _free_gb():
    st = os.statvfs("/")
    return st.f_bavail * st.f_frsize / 1e9


async def main(dfrom, dto, limit, min_disk, skip_ai=False):
    logger.info(f"=== catchup-date [{dfrom},{dto}) limit={limit} AI_CHUNK={AI_CHUNK} skip_ai={skip_ai} disk={_free_gb():.1f}GB ===")
    if skip_ai:
        logger.info("[1] --skip-ai: AI 단계 건너뜀 (이미 ai_processed 된 건만 리스팅)")
    else:
        with get_db() as conn:
            ai_pids = [r["id"] for r in conn.execute(
                "SELECT id FROM products WHERE created_at>=? AND created_at<? AND ai_processed_at IS NULL "
                "AND cost_usd>0 ORDER BY id LIMIT ?", (dfrom, dto, limit or 1000000)).fetchall()]
        logger.info(f"[1] AI 미처리: {len(ai_pids)}건")
        from backend.purchase.services.ai_processor import run_two_stage_batch
        done = 0
        for i in range(0, len(ai_pids), AI_CHUNK):
            if _free_gb() < min_disk:
                logger.warning(f"★디스크 {_free_gb():.1f}GB < {min_disk} — AI 중단"); break
            chunk = ai_pids[i:i + AI_CHUNK]
            job = uuid.uuid4().hex[:12]
            with get_db() as conn:
                conn.execute("INSERT INTO batch_jobs (id, job_type, status, total, created_at) "
                             "VALUES (?, 'ai_detail', 'pending', ?, ?)", (job, len(chunk), _now_iso()))
            try:
                await run_two_stage_batch(job, chunk)
            except Exception as e:
                logger.warning(f"[1] chunk {i} AI 예외(계속): {e}")
            done += len(chunk)
            if done % 150 == 0 or done >= len(ai_pids):
                logger.info(f"[1] AI {done}/{len(ai_pids)} (disk {_free_gb():.1f}GB)")
        logger.info("[1] AI 완료")

    from backend.purchase.services.channel_listing_service import send_to_channels
    with get_db() as conn:
        ready = [r["id"] for r in conn.execute(
            "SELECT p.id FROM products p WHERE p.created_at>=? AND p.created_at<? AND p.ai_processed_at IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM listings_pa l WHERE l.product_id=p.id AND l.channel='coupang' "
            "AND l.status IN ('listed','pending')) ORDER BY p.id", (dfrom, dto)).fetchall()]
    logger.info(f"[2] 채널 전송 대상: {len(ready)}건")
    sent = failed = 0
    for i, pid in enumerate(ready, 1):
        try:
            await asyncio.to_thread(send_to_channels, pid, ["coupang"])
            sent += 1
        except Exception as e:
            failed += 1
            if failed <= 15:
                logger.warning(f"[2] pid={pid} fail: {e}")
        if i % 100 == 0:
            logger.info(f"[2] {i}/{len(ready)} sent={sent} failed={failed}")
    logger.info(f"[2] 채널 전송 완료 sent={sent} failed={failed}")

    from backend.purchase.routers.coupang import _run_coupang_upload_bg
    with get_db() as conn:
        cu = [r["product_id"] for r in conn.execute(
            "SELECT DISTINCT l.product_id FROM listings_pa l JOIN products p ON p.id=l.product_id "
            "WHERE l.channel='coupang' AND l.status='pending' AND l.channel_product_id IS NULL "
            "AND p.created_at>=? AND p.created_at<?", (dfrom, dto)).fetchall()]
    if not cu:
        logger.info("[3] coupang pending 0 — skip"); logger.info("=== 완료 ==="); return
    job2 = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute("INSERT INTO batch_jobs (id, job_type, status, total, created_at) "
                     "VALUES (?, 'coupang_upload', 'pending', ?, ?)", (job2, len(cu), _now_iso()))
    logger.info(f"[3] coupang upload 시작 job={job2} count={len(cu)}")
    await _run_coupang_upload_bg(job2, cu)
    with get_db() as conn:
        ph = ",".join("?" * len(cu))
        listed = conn.execute(f"SELECT COUNT(*) FROM listings_pa WHERE channel='coupang' AND status='listed' AND product_id IN ({ph})", cu).fetchone()[0]
        excl = conn.execute(f"SELECT COUNT(*) FROM listings_pa WHERE channel='coupang' AND status='excluded' AND product_id IN ({ph})", cu).fetchone()[0]
    logger.info(f"[3] 완료 — listed={listed} excluded(필터차단)={excl} of {len(cu)}")
    logger.info("=== 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dfrom", required=True)
    ap.add_argument("--to", dest="dto", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-disk-gb", type=float, default=6.0)
    ap.add_argument("--skip-ai", action="store_true", help="AI 단계 건너뛰고 이미 ai_processed 된 건만 리스팅")
    args = ap.parse_args()
    try:
        asyncio.run(main(args.dfrom, args.dto, args.limit, args.min_disk_gb, args.skip_ai))
    except Exception:
        logger.exception("=== 예외 ===")
        sys.exit(1)
