"""sid=18 정리 — 2026-05-21 import 4,697 draft 를 archived 처리 + sheet_queue 닫기.
사유: 97.7%(4,588)가 이미 다른 pid로 listed된 중복, 109건만 신규였으나 사용자 지시로 일괄 정리.
복구: status='draft' 로 되돌리면 catchup_sid18 로 재처리 가능 (특히 unique 109건)."""
import argparse
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend.purchase.database import get_db
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("archive-sid18")

FROM, TO = "2026-05-21", "2026-05-22"


def main(apply):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_db() as c:
        n_draft = c.execute("SELECT COUNT(*) FROM products WHERE created_at>=? AND created_at<? AND status='draft'",
                            (FROM, TO)).fetchone()[0]
        sq = c.execute("SELECT status FROM sheet_queue WHERE id=18").fetchone()
    logger.info(f"대상 draft: {n_draft}건 | sheet_queue sid18 현재: {sq['status'] if sq else 'N/A'}")
    if not apply:
        logger.info("=== dry-run (--apply 로 실행) ===")
        return
    with get_db() as c:
        cur = c.execute(
            "UPDATE products SET status='archived', updated_at=? "
            "WHERE created_at>=? AND created_at<? AND status='draft'", (now, FROM, TO))
        n_arch = cur.rowcount
        c.execute(
            "UPDATE sheet_queue SET status='cancelled', finished_at=?, "
            "current_step='중복 정리(4588/4697 이미 listed) — archived' WHERE id=18", (now,))
    logger.info(f"✓ products archived: {n_arch}건, sheet_queue sid18 → cancelled")
    logger.info("=== 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    main(ap.parse_args().apply)
