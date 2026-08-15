"""discovered 후보 중 이미 products 에 promote 된 ASIN 삭제 — 중복 promote 방지.

배경: 디스커버리가 이미 등록된 ASIN 을 재수집해 sourcing_candidates 에 쌓임.
run_promote_background 는 후보 전체를 처리하므로, 이미 products 에 있는 ASIN 을
그대로 두면 promote 시 중복 product 양산(dangling-FK 부채 재발). 날짜컷
(clean_stale_candidates) 으로는 같은 날 수집분을 못 거르므로 ASIN-exists 기준으로 청소.

  python -m backend.purchase.scripts.clean_discovered_dup_asin --dry-run
  python -m backend.purchase.scripts.clean_discovered_dup_asin --apply
"""
import argparse, os, logging
from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend.purchase.database import get_db
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)  # 단발 스크립트 db_factory 등록 (feedback_db_factory_nohup)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("clean-dup-asin")


def main(apply):
    with get_db() as c:
        total = c.execute(
            "SELECT COUNT(*) FROM sourcing_candidates WHERE sourcing_status='discovered'"
        ).fetchone()[0]
        dup = c.execute(
            "SELECT COUNT(*) FROM sourcing_candidates "
            "WHERE sourcing_status='discovered' AND asin IN (SELECT asin FROM products)"
        ).fetchone()[0]
        keep = total - dup
    logger.info(f"discovered {total} | 이미 products 존재(삭제대상) {dup} | 신규 유지 {keep}")
    if not apply:
        logger.info("=== dry-run (--apply 로 실행) ===")
        return
    with get_db() as c:
        n = c.execute(
            "DELETE FROM sourcing_candidates "
            "WHERE sourcing_status='discovered' AND asin IN (SELECT asin FROM products)"
        ).rowcount
    logger.info(f"✓ 중복 ASIN 삭제: {n}건 | 남은 신규 discovered: {keep}")
    logger.info("=== 완료 ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    main(args.apply)
