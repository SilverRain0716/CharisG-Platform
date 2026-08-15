"""stale sourcing_candidates 정리 — sid 20(05-25) 재promote 전, 옛 배치(05-23) 잔재 삭제.
05-23 배치 4,967건 중 96%가 이미 products에 promote됨(sid 18~ 잔재). run_promote_background이
candidates 전체를 처리하므로, sid 20만 깔끔히 + 락노출 최소화 위해 옛 것 제거.
삭제 기준: collected_at < CUTOFF (기본 2026-05-25)."""
import argparse
import logging
import os
from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend.purchase.database import get_db
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("clean-stale")


def main(cutoff, apply):
    with get_db() as c:
        before = c.execute("SELECT COUNT(*) FROM sourcing_candidates").fetchone()[0]
        stale = c.execute("SELECT COUNT(*) FROM sourcing_candidates WHERE collected_at < ?", (cutoff,)).fetchone()[0]
        keep = c.execute("SELECT COUNT(*) FROM sourcing_candidates WHERE collected_at >= ?", (cutoff,)).fetchone()[0]
    logger.info(f"전체 {before} | 삭제대상(<{cutoff}) {stale} | 유지(>={cutoff}) {keep}")
    if not apply:
        logger.info("=== dry-run (--apply 로 실행) ==="); return
    with get_db() as c:
        n = c.execute("DELETE FROM sourcing_candidates WHERE collected_at < ?", (cutoff,)).rowcount
    logger.info(f"✓ stale 삭제: {n}건 | 남은 sourcing_candidates: {keep}")
    logger.info("=== 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default="2026-05-25")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    main(args.cutoff, args.apply)
