"""sid=10 streaming 사고 후 sequential catch-up — 4단계 직렬 진행.

streaming pipeline 의 다중 process 사고로 인한 잔여 정리.
- 1,740 sourcing_candidates → promote
- 7,617+ products draft (ai_processed_at NULL) → detail
- ai 처리 끝났지만 listings_pa 없는 products → channel listing
- listings_pa.status='pending' AND channel='coupang' → coupang_upload (5K 한도)

각 단계 batch_jobs INSERT + 진행률 추적. 단일 process 보장 (PID 1개).

실행:
  nohup python -m backend.purchase.scripts.sequential_catchup \\
      > /tmp/sequential_catchup.log 2>&1 &

각 단계 완료 후 별도 catch-up 가능 (단계 인자 받음).
"""
import argparse
import asyncio
import logging
import os
import sys
import uuid

# .env 로드 — nohup 별도 process 라 systemd EnvironmentFile 못 받음
from dotenv import load_dotenv
_ROOT = os.environ.get(
    "CHARISG_ROOT",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
)
load_dotenv(os.path.join(_ROOT, ".env"))

from backend.purchase.database import get_db
# 2026-06-02: standalone(nohup) 진입 시 db_factory 등록 필수 — ai_processor S2 등이
# backend_shared.context.get_db 사용. 미등록 시 "db_factory가 등록되지 않았습니다" 로 AI S2 전멸.
from backend_shared.context import register_db_factory
register_db_factory(get_db)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("seq-catchup")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def step1_promote() -> int:
    """1,740 sourcing_candidates → promote (기존 sourcing_promote 호출)."""
    from backend.purchase.services.sourcing_promote import (
        create_promote_job, run_promote_background,
    )
    with get_db() as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM sourcing_candidates").fetchone()[0]
    if cnt == 0:
        logger.info("[step1] sourcing_candidates 0건 — skip")
        return 0
    logger.info(f"[step1] promote 시작 — sourcing_candidates {cnt}건")
    promote_job = create_promote_job(cnt)
    await run_promote_background(promote_job)
    with get_db() as conn:
        new_cnt = conn.execute("SELECT processed FROM batch_jobs WHERE id=?", (promote_job,)).fetchone()[0]
    logger.info(f"[step1] promote 완료 — promote_job={promote_job} processed={new_cnt}")
    return new_cnt


async def step2_detail() -> int:
    """모든 ai_processed_at IS NULL draft products → detail."""
    from backend.purchase.services.ai_processor import run_two_stage_batch
    with get_db() as conn:
        pids = [r["id"] for r in conn.execute(
            "SELECT id FROM products WHERE status='draft' AND business_model='purchase' "
            "AND ai_processed_at IS NULL ORDER BY id"
        ).fetchall()]
    if not pids:
        logger.info("[step2] 미처리 draft 0건 — skip")
        return 0
    logger.info(f"[step2] detail 시작 — {len(pids)}건")
    detail_job = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO batch_jobs (id, job_type, status, total, created_at) "
            "VALUES (?, 'ai_detail', 'pending', ?, ?)",
            (detail_job, len(pids), _now_iso()),
        )
    await run_two_stage_batch(detail_job, pids)
    with get_db() as conn:
        done = conn.execute("SELECT processed FROM batch_jobs WHERE id=?", (detail_job,)).fetchone()[0]
    logger.info(f"[step2] detail 완료 — job={detail_job} processed={done}")
    return done


async def step3_listing(channels: list[str]) -> int:
    """ai_processed_at NOT NULL + listings_pa 없는 products → send_to_channels."""
    from backend.purchase.services.channel_listing_service import send_to_channels
    with get_db() as conn:
        ph = ",".join(f"'{c}'" for c in channels)
        pids = [r["id"] for r in conn.execute(
            f"""SELECT p.id FROM products p
                WHERE p.status='draft' AND p.business_model='purchase'
                  AND p.ai_processed_at IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM listings_pa l
                     WHERE l.product_id=p.id AND l.channel IN ({ph})
                  )
                ORDER BY p.id"""
        ).fetchall()]
    if not pids:
        logger.info("[step3] listings_pa 없는 products 0건 — skip")
        return 0
    logger.info(f"[step3] channel listing 시작 — {len(pids)}건 channels={channels}")
    sent = 0
    failed = 0
    for i, pid in enumerate(pids, 1):
        try:
            await asyncio.to_thread(send_to_channels, pid, channels)
            sent += 1
        except Exception as e:
            failed += 1
            if failed <= 5:
                logger.warning(f"[step3] pid={pid} fail: {e}")
        if i % 100 == 0:
            logger.info(f"[step3] {i}/{len(pids)} — sent={sent} failed={failed}")
    logger.info(f"[step3] channel listing 완료 — sent={sent} failed={failed}")
    return sent


async def step4_upload() -> int:
    """listings_pa.status='pending' AND channel='coupang' → coupang_upload."""
    from backend.purchase.routers.coupang import _run_coupang_upload_bg
    with get_db() as conn:
        cu_pids = [r["product_id"] for r in conn.execute(
            "SELECT DISTINCT product_id FROM listings_pa "
            "WHERE channel='coupang' AND status='pending' "
            "AND channel_product_id IS NULL"
        ).fetchall()]
    if not cu_pids:
        logger.info("[step4] coupang pending 0건 — skip")
        return 0
    logger.info(f"[step4] coupang upload 시작 — {len(cu_pids)}건 (5K/일 한도 자동 적용)")
    cu_job = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO batch_jobs (id, job_type, status, total, created_at) "
            "VALUES (?, 'coupang_upload', 'pending', ?, ?)",
            (cu_job, len(cu_pids), _now_iso()),
        )
    await _run_coupang_upload_bg(cu_job, cu_pids)
    with get_db() as conn:
        listed = conn.execute(
            f"""SELECT COUNT(*) FROM listings_pa
                WHERE channel='coupang' AND status='listed'
                  AND product_id IN ({','.join('?' * len(cu_pids))})""",
            cu_pids,
        ).fetchone()[0]
    logger.info(f"[step4] coupang upload 완료 — job={cu_job} listed={listed}/{len(cu_pids)}")
    return listed


async def main(steps: list[int], channels: list[str]) -> None:
    logger.info(f"=== sequential catch-up 시작 — steps={steps} channels={channels} ===")
    if 1 in steps:
        await step1_promote()
    if 2 in steps:
        await step2_detail()
    if 3 in steps:
        await step3_listing(channels)
    if 4 in steps:
        await step4_upload()
    logger.info("=== sequential catch-up 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="1,2,3,4",
                    help="실행할 단계 CSV (default: 1,2,3,4)")
    ap.add_argument("--channels", default="coupang")
    args = ap.parse_args()
    steps = [int(s) for s in args.steps.split(",")]
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    try:
        asyncio.run(main(steps, channels))
    except Exception:
        logger.exception("=== sequential catch-up 예외 ===")
        sys.exit(1)
