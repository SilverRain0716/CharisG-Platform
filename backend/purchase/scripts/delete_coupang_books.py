"""
delete_coupang_books.py — 도서(책) 카테고리로 잘못 등록된 쿠팡 listed 상품 일괄 정리.

대상: listings_pa.channel='coupang' AND status='listed'
      AND naver_categories.whole_name LIKE '도서%'

처리:
  1. stop_sales(seller_product_id) — 판매중지 (delete 는 임시저장만 가능)
  2. listings_pa.status='excluded', error_message='도서 카테고리 정리 (2026-05-01)'

사용:
  .venv/bin/python3 -m backend.purchase.scripts.delete_coupang_books --dry-run
  .venv/bin/python3 -m backend.purchase.scripts.delete_coupang_books --execute
"""
import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from backend.purchase.services.coupang_service import stop_sales

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

REASON = "도서 카테고리 정리 (2026-05-01)"
INTERVAL = 0.3  # 쿠팡 rate limit 보호


def fetch_targets(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        """SELECT l.id, l.product_id, l.channel_product_id, p.asin, p.title_en, nc.whole_name
           FROM listings_pa l
           JOIN products p ON p.id = l.product_id
           JOIN naver_categories nc ON nc.id = l.category_mapped
           WHERE l.channel='coupang' AND l.status='listed'
             AND l.channel_product_id IS NOT NULL AND l.channel_product_id <> ''
             AND nc.whole_name LIKE '도서%'
           ORDER BY l.id"""
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    if not args.dry_run and not args.execute:
        ap.error("--dry-run 또는 --execute 필요")

    db = Path(__file__).resolve().parents[1] / "purchase.db"
    conn = sqlite3.connect(str(db))
    targets = fetch_targets(conn)
    logger.info(f"=== 대상: {len(targets)}건 ===")

    if args.dry_run:
        for t in targets[:10]:
            logger.info(f"  [DRY] id={t['id']} spid={t['channel_product_id']} asin={t['asin']} | {(t['title_en'] or '')[:60]}")
        logger.info(f"  ... 총 {len(targets)}건 (dry-run)")
        return

    ok = 0
    fail = 0
    fail_msgs = []
    for i, t in enumerate(targets, 1):
        spid = str(t["channel_product_id"])
        success, err = stop_sales(spid)
        if success:
            ok += 1
            conn.execute(
                """UPDATE listings_pa
                   SET status='excluded', error_message=?, last_synced_at=datetime('now')
                   WHERE id=?""",
                (REASON, t["id"]),
            )
            conn.commit()
        else:
            fail += 1
            if len(fail_msgs) < 20:
                fail_msgs.append((spid, err))
        if i % 10 == 0 or i == len(targets):
            logger.info(f"  progress {i}/{len(targets)} ok={ok} fail={fail}")
        if i < len(targets):
            time.sleep(INTERVAL)

    logger.info(f"=== 완료: 정지+excluded {ok}, 실패 {fail} ===")
    for spid, err in fail_msgs:
        logger.warning(f"  spid={spid}: {err[:200]}")


if __name__ == "__main__":
    main()
