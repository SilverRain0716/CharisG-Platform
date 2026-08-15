"""sid=16 coupang pending 업로드 — 디스크 안전 소배치.

catchup 으로 AI+send 까지 끝나 coupang pending 9,700 존재. 업로드 Phase 0 가 이미지를
재다운로드하므로 한방에 9,700 하면 디스크풀 → --limit 으로 분할 (쿠팡 5K/일 한도 + 디스크).

실행:
  nohup .venv/bin/python -m backend.purchase.scripts.upload_coupang_batch --limit 4800 \\
      > /tmp/upload_cu.log 2>&1 &
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
logger = logging.getLogger("upload-cu-batch")

CREATED_SINCE = "2026-05-23"


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def main(limit: int) -> None:
    with get_db() as conn:
        cu_pids = [r["product_id"] for r in conn.execute(
            """SELECT DISTINCT l.product_id FROM listings_pa l
               JOIN products p ON p.id = l.product_id
               WHERE l.channel='coupang' AND l.status='pending' AND l.channel_product_id IS NULL
                 AND p.created_at >= ?
               ORDER BY l.product_id LIMIT ?""",
            (CREATED_SINCE, limit),
        ).fetchall()]
    logger.info(f"coupang 업로드 대상: {len(cu_pids)}건 (limit={limit})")
    if not cu_pids:
        logger.info("대상 0 — 종료 (=== 완료 ===)"); return

    from backend.purchase.routers.coupang import _run_coupang_upload_bg
    job = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            "INSERT INTO batch_jobs (id, job_type, status, total, created_at) "
            "VALUES (?, 'coupang_upload', 'pending', ?, ?)",
            (job, len(cu_pids), _now_iso()),
        )
    logger.info(f"upload 시작 — job={job}")
    await _run_coupang_upload_bg(job, cu_pids)

    with get_db() as conn:
        ph = ",".join("?" * len(cu_pids))
        listed = conn.execute(
            f"SELECT COUNT(*) FROM listings_pa WHERE channel='coupang' AND status='listed' AND product_id IN ({ph})",
            cu_pids,
        ).fetchone()[0]
        pend = conn.execute(
            f"SELECT COUNT(*) FROM listings_pa WHERE channel='coupang' AND status='pending' AND product_id IN ({ph})",
            cu_pids,
        ).fetchone()[0]
        exc = conn.execute(
            f"SELECT COUNT(*) FROM listings_pa WHERE channel='coupang' AND status='excluded' AND product_id IN ({ph})",
            cu_pids,
        ).fetchone()[0]
    logger.info(f"완료 — listed={listed} pending={pend} excluded={exc}")
    logger.info("=== 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=4800)
    args = ap.parse_args()
    try:
        asyncio.run(main(args.limit))
    except Exception:
        logger.exception("=== 예외 ===")
        sys.exit(1)
