"""sid=27 catch-up — 2026-06-10 pa-api 재시작(락수정 배포)으로 detailing 고아된 가전시트 재개.

catchup_sid18.py 기반. sid 27 = 2026-06-09 23:46 import 분 (가전부속·필터·자동차).
배경: id=27 detailing batch(7387) 가 750/7387 에서 pa-api 재시작에 죽음 → 시트워커는
  'queued' 만 재pick 하므로 'detailing' 고아. 재큐는 promote dedup(신규0)→done 으로
  남은 detailing 유실시킴 → 직접 catchup.

  - 대상 = products created_at in [2026-06-09T23:46, 2026-06-11) ← id27 유입분 + 그룹워커 sibling
  - title_en/이미지/가격(landed)/weight 는 이미 import 됨 → 누락분(AI 상세/카테고리)만 처리
  - AI(run_two_stage_batch) CHUNK(30)씩 → DB 락 회피
  - ★리스팅 = 단독(parent_asin NULL)만 coupang single. 변형은 group_registration_queue(w1/w2)
    가 이미 처리 중이라 건드리지 않음(중복/충돌 방지).
  - KC/중복/safety/brand 게이트는 send_to_channels + coupang_lister 자동 작동.
  - idempotent: ai_processed_at / coupang listing 존재분 자동 skip → 재실행 안전.

실행:
  nohup .venv/bin/python -m backend.purchase.scripts.catchup_sid27 --limit 8000 > /tmp/catchup_sid27.log 2>&1 &
"""
import argparse
import asyncio
import logging
import os
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
logger = logging.getLogger("catchup-sid27")

CREATED_FROM = "2026-06-09T23:46:00Z"   # id 27 import 시작
CREATED_TO = "2026-06-11"               # 배타적 상한
AI_CHUNK = 30


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def main(limit: int) -> None:
    logger.info(f"=== sid=27 catch-up — created in [{CREATED_FROM},{CREATED_TO}) limit={limit} AI_CHUNK={AI_CHUNK} ===")

    # 1. AI 미처리분 → CHUNK 분할 detailing
    # ★단독(parent NULL)만 detailing — 변형은 그룹큐(w1/w2)가 자식상세 없이 등록 중 +
    #   스마트스토어 중지(06-08)라 변형 단일상세 불필요. 낭비 detailing 회피.
    with get_db() as conn:
        ai_pids = [r["id"] for r in conn.execute(
            """SELECT id FROM products
                WHERE created_at >= ? AND created_at < ? AND ai_processed_at IS NULL AND cost_usd > 0
                  AND (parent_asin IS NULL OR parent_asin='')
                ORDER BY id LIMIT ?""",
            (CREATED_FROM, CREATED_TO, limit),
        ).fetchall()]
    logger.info(f"[1] AI 미처리 대상(단독): {len(ai_pids)}건 → {AI_CHUNK}건씩 분할")
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
                    "SELECT COUNT(*) FROM products WHERE created_at>=? AND created_at<? AND ai_processed_at IS NOT NULL",
                    (CREATED_FROM, CREATED_TO),
                ).fetchone()[0]
            logger.info(f"[1] AI 진행 {done}/{len(ai_pids)} (누적 ai_processed={ai_ok})")
    logger.info(f"[1] AI 완료")

    # 2. 리스팅 — ★단독(parent_asin NULL)만 coupang single. 변형은 group-worker 처리중이라 제외.
    from backend.purchase.services.channel_listing_service import send_to_channels
    with get_db() as conn:
        ready = [r["id"] for r in conn.execute(
            """SELECT p.id FROM products p
                WHERE p.created_at >= ? AND p.created_at < ? AND p.ai_processed_at IS NOT NULL
                  AND (p.parent_asin IS NULL OR p.parent_asin='')
                  AND NOT EXISTS (
                    SELECT 1 FROM listings_pa l
                     WHERE l.product_id=p.id AND l.channel='coupang'
                       AND l.status IN ('listed','pending'))
                ORDER BY p.id""",
            (CREATED_FROM, CREATED_TO),
        ).fetchall()]
    logger.info(f"[2] coupang single 리스팅 대상(단독): {len(ready)}건")
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
    logger.info(f"[2] 리스팅 완료 — sent={sent} failed={failed}")

    # 3. coupang upload (5K 한도 자동; KC/중복 필터 작동) — 단독 pending만
    from backend.purchase.routers.coupang import _run_coupang_upload_bg
    with get_db() as conn:
        cu_pids = [r["product_id"] for r in conn.execute(
            """SELECT DISTINCT l.product_id FROM listings_pa l
                JOIN products p ON p.id=l.product_id
                WHERE l.channel='coupang' AND l.status='pending' AND l.channel_product_id IS NULL
                  AND (p.parent_asin IS NULL OR p.parent_asin='')
                  AND p.created_at >= ? AND p.created_at < ?""",
            (CREATED_FROM, CREATED_TO),
        ).fetchall()]
    if not cu_pids:
        logger.info("[3] coupang pending(단독) 0건 — skip")
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
    logger.info("=== catch-up 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=8000)
    a = ap.parse_args()
    asyncio.run(main(a.limit))
