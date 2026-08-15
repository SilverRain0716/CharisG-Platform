"""sid=16 catch-up — 5/23 promote 후 detailing 중 디스크풀로 멈춘 ~9,911건 listing 재개.

catchup_sid10.py 기반. 핵심 설계:
  - 대상 = products created_at >= '2026-05-23'
  - ★AI(run_two_stage_batch)를 CHUNK(기본 30)씩 분할 호출 → 동시 DB write 락 회피
    (4,500 한방 gather 는 'database is locked' 크래시 — 2026-05-24 사고)
  - detail_pages DELETE 안 함 → Stage1(이미지) skip → 이미지 재다운 없음(디스크 보호)
  - 리스팅 대상 = ai_processed_at NOT NULL AND coupang listing(listed/pending) 없음
    → 크래시 stragglers + 신규 AI분 모두 포착, 재실행 idempotent
  - 채널 = coupang (5K/일 한도; 초과분 lister 가 pending → quota_retry 익일)
  - KC/중복/safety 게이트는 send_to_channels + coupang_lister 에서 자동 작동.

실행:
  nohup .venv/bin/python -m backend.purchase.scripts.catchup_sid16 --limit 5000 \\
      > /tmp/catchup_sid16.log 2>&1 &
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("catchup-sid16")

CREATED_SINCE = "2026-05-23"
AI_CHUNK = 30  # run_two_stage_batch 동시성 — DB 락 회피


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def main(limit: int) -> None:
    logger.info(f"=== sid=16 catch-up — created>={CREATED_SINCE} limit={limit} AI_CHUNK={AI_CHUNK} ===")

    # 1. AI 미처리분 SELECT (limit) → CHUNK 분할 처리
    with get_db() as conn:
        ai_pids = [r["id"] for r in conn.execute(
            f"""SELECT id FROM products
                WHERE created_at >= ? AND ai_processed_at IS NULL AND cost_usd > 0
                ORDER BY id LIMIT ?""",
            (CREATED_SINCE, limit),
        ).fetchall()]
    logger.info(f"[1] AI 미처리 대상: {len(ai_pids)}건 → {AI_CHUNK}건씩 분할")
    from backend.purchase.services.ai_processor import run_two_stage_batch
    done = 0
    for i in range(0, len(ai_pids), AI_CHUNK):
        chunk = ai_pids[i:i + AI_CHUNK]
        job = uuid.uuid4().hex[:12]
        with get_db() as conn:
            conn.execute(
                "INSERT INTO batch_jobs (id, job_type, status, total, created_at) "
                "VALUES (?, 'ai_detail', 'pending', ?, ?)",
                (job, len(chunk), _now_iso()),
            )
        try:
            await run_two_stage_batch(job, chunk)
        except Exception as e:
            logger.warning(f"[1] chunk {i}~{i+len(chunk)} AI 예외 (계속): {e}")
        done += len(chunk)
        if done % 300 == 0 or done >= len(ai_pids):
            with get_db() as conn:
                ai_ok = conn.execute(
                    f"SELECT COUNT(*) FROM products WHERE created_at>=? AND ai_processed_at IS NOT NULL",
                    (CREATED_SINCE,),
                ).fetchone()[0]
            logger.info(f"[1] AI 진행 {done}/{len(ai_pids)} (누적 ai_processed={ai_ok})")
    logger.info(f"[1] AI 완료")

    # 2. 리스팅 대상 = ai 완료 + coupang listing(listed/pending) 없음 (stragglers 포함)
    from backend.purchase.services.channel_listing_service import send_to_channels
    with get_db() as conn:
        ready = [r["id"] for r in conn.execute(
            f"""SELECT p.id FROM products p
                WHERE p.created_at >= ? AND p.ai_processed_at IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM listings_pa l
                     WHERE l.product_id=p.id AND l.channel='coupang'
                       AND l.status IN ('listed','pending'))
                ORDER BY p.id""",
            (CREATED_SINCE,),
        ).fetchall()]
    logger.info(f"[2] channel listing(coupang) 대상: {len(ready)}건")
    sent = failed = 0
    for i, pid in enumerate(ready, 1):
        try:
            await asyncio.to_thread(send_to_channels, pid, ["coupang"])
            sent += 1
        except Exception as e:
            failed += 1
            if failed <= 15:
                logger.warning(f"[2] pid={pid} fail: {e}")
        if i % 200 == 0:
            logger.info(f"[2] {i}/{len(ready)} — sent={sent} failed={failed}")
    logger.info(f"[2] channel listing 완료 — sent={sent} failed={failed}")

    # 3. coupang upload (5K 한도 자동; KC/중복 필터 작동)
    from backend.purchase.routers.coupang import _run_coupang_upload_bg
    with get_db() as conn:
        cu_pids = [r["product_id"] for r in conn.execute(
            f"""SELECT DISTINCT l.product_id FROM listings_pa l
                JOIN products p ON p.id=l.product_id
                WHERE l.channel='coupang' AND l.status='pending' AND l.channel_product_id IS NULL
                  AND p.created_at >= ?""",
            (CREATED_SINCE,),
        ).fetchall()]
    if not cu_pids:
        logger.info("[3] coupang pending 0건 — skip")
        logger.info("=== catch-up 완료 ===")
        return
    job2 = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO batch_jobs (id, job_type, status, total, created_at) "
            "VALUES (?, 'coupang_upload', 'pending', ?, ?)",
            (job2, len(cu_pids), _now_iso()),
        )
    logger.info(f"[3] coupang upload 시작 — job={job2} count={len(cu_pids)}")
    await _run_coupang_upload_bg(job2, cu_pids)
    with get_db() as conn:
        ph2 = ",".join("?" * len(cu_pids))
        listed_cnt = conn.execute(
            f"SELECT COUNT(*) FROM listings_pa WHERE channel='coupang' AND status='listed' AND product_id IN ({ph2})",
            cu_pids,
        ).fetchone()[0]
        pending_cnt = conn.execute(
            f"SELECT COUNT(*) FROM listings_pa WHERE channel='coupang' AND status='pending' AND product_id IN ({ph2})",
            cu_pids,
        ).fetchone()[0]
    logger.info(f"[3] coupang upload 완료 — listed={listed_cnt} pending={pending_cnt} (5K 한도 시 pending → 내일 quota_retry)")
    logger.info("=== catch-up 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5000, help="이번 실행 AI 처리 상한 (쿠팡 listing 은 5K/일 자동 제한)")
    args = ap.parse_args()
    try:
        asyncio.run(main(args.limit))
    except Exception:
        logger.exception("=== catch-up 예외 ===")
        sys.exit(1)
