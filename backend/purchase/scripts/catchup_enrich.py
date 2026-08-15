"""enrichment 배치(enrich_pids.csv) 처리 — AI→카테고리→쿠팡 리스팅.

catchup_sid18 기반. 대상 = /tmp/enrich_pids.csv 의 product_id (오늘 enrich 한 1,099건).
  - AI(run_two_stage_batch) CHUNK 30 → DB 락 회피
  - 리스팅 = ai 완료 + coupang listing 없음 (idempotent)
  - 채널 = coupang. 리스크 필터(금지성분/IP/한국mfr/KC/중복)는 list_product 에서 자동 차단.
  - 디스크 가드: 매 청크 전 여유 체크, min 미만이면 중단.

실행:
  .venv/bin/python -m backend.purchase.scripts.catchup_enrich --limit 10   # 검증
  nohup .venv/bin/python -m backend.purchase.scripts.catchup_enrich > /tmp/catchup_enrich.log 2>&1 &
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
PIDS = os.environ.get("ENRICH_PIDS", "/tmp/enrich_pids.csv")
AI_CHUNK = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("catchup-enrich")


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _free_gb():
    st = os.statvfs("/")
    return st.f_bavail * st.f_frsize / 1e9


async def main(limit, min_disk):
    with open(PIDS) as f:
        all_pids = [int(x) for x in f.read().split() if x.strip()]
    if limit:
        all_pids = all_pids[:limit]
    ph = ",".join("?" * len(all_pids))
    logger.info(f"=== catchup-enrich — 대상 {len(all_pids)}건 AI_CHUNK={AI_CHUNK} disk={_free_gb():.1f}GB ===")

    # 1. AI 미처리분
    with get_db() as conn:
        ai_pids = [r["id"] for r in conn.execute(
            f"SELECT id FROM products WHERE id IN ({ph}) AND ai_processed_at IS NULL AND cost_usd>0 ORDER BY id",
            all_pids).fetchall()]
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

    # 2. 채널 리스팅 (리스크 필터 자동 작동)
    from backend.purchase.services.channel_listing_service import send_to_channels
    with get_db() as conn:
        ready = [r["id"] for r in conn.execute(
            f"SELECT p.id FROM products p WHERE p.id IN ({ph}) AND p.ai_processed_at IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM listings_pa l WHERE l.product_id=p.id AND l.channel='coupang' "
            f"AND l.status IN ('listed','pending')) ORDER BY p.id", all_pids).fetchall()]
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

    # 3. coupang upload (리스크 필터/5K 한도 자동)
    from backend.purchase.routers.coupang import _run_coupang_upload_bg
    with get_db() as conn:
        cu = [r["product_id"] for r in conn.execute(
            f"SELECT DISTINCT l.product_id FROM listings_pa l WHERE l.product_id IN ({ph}) "
            f"AND l.channel='coupang' AND l.status='pending' AND l.channel_product_id IS NULL", all_pids).fetchall()]
    if not cu:
        logger.info("[3] coupang pending 0 — skip"); logger.info("=== 완료 ==="); return
    job2 = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute("INSERT INTO batch_jobs (id, job_type, status, total, created_at) "
                     "VALUES (?, 'coupang_upload', 'pending', ?, ?)", (job2, len(cu), _now_iso()))
    logger.info(f"[3] coupang upload 시작 job={job2} count={len(cu)}")
    await _run_coupang_upload_bg(job2, cu)
    with get_db() as conn:
        ph2 = ",".join("?" * len(cu))
        listed = conn.execute(f"SELECT COUNT(*) FROM listings_pa WHERE channel='coupang' AND status='listed' AND product_id IN ({ph2})", cu).fetchone()[0]
        pend = conn.execute(f"SELECT COUNT(*) FROM listings_pa WHERE channel='coupang' AND status='pending' AND product_id IN ({ph2})", cu).fetchone()[0]
        excl = conn.execute(f"SELECT COUNT(*) FROM listings_pa WHERE channel='coupang' AND status='excluded' AND product_id IN ({ph2})", cu).fetchone()[0]
    logger.info(f"[3] 완료 — listed={listed} pending={pend} excluded(필터차단)={excl}")
    logger.info("=== 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-disk-gb", type=float, default=6.0)
    args = ap.parse_args()
    try:
        asyncio.run(main(args.limit, args.min_disk_gb))
    except Exception:
        logger.exception("=== 예외 ===")
        sys.exit(1)
