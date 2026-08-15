"""
delete_naver_by_category.py — 네이버 스마트스토어 listed 상품을 카테고리 키워드로 일괄 완전삭제.

대상: listings_pa.channel='smartstore' AND status='listed'
      AND naver_categories.whole_name LIKE 키워드

처리:
1. listings_pa.channel_product_id 가 originProductNo 이므로 search 매핑 불필요
2. DELETE /v2/products/origin-products/{originProductNo}
3. 성공 시 listings_pa.status='excluded', error_message=<reason>

사용:
    python3 -m backend.purchase.scripts.delete_naver_by_category --dry-run
    python3 -m backend.purchase.scripts.delete_naver_by_category --execute
"""
import argparse
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from backend.purchase.services.naver_commerce_service import delete_product

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

KEYWORDS = ["원예", "정원", "식물", "화분"]
REASON = "원예/정원/식물/화분 카테고리 정리 (2026-05-01)"


def fetch_targets(conn: sqlite3.Connection) -> list[dict]:
    where_kw = " OR ".join([f"nc.whole_name LIKE '%{k}%'" for k in KEYWORDS])
    sql = f"""
    SELECT l.id, l.product_id, l.channel_product_id, nc.whole_name, p.asin, p.title_ko
    FROM listings_pa l
    JOIN naver_categories nc ON nc.id = l.category_mapped
    LEFT JOIN products p ON p.id = l.product_id
    WHERE l.channel='smartstore'
      AND l.status='listed'
      AND l.channel_product_id IS NOT NULL
      AND l.channel_product_id <> ''
      AND ({where_kw})
    ORDER BY nc.whole_name, l.id
    """
    cur = conn.cursor()
    cur.execute(sql)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="대상만 출력")
    ap.add_argument("--execute", action="store_true", help="실제 삭제 수행")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    if not args.dry_run and not args.execute:
        ap.error("--dry-run 또는 --execute 중 하나를 지정하세요")
    if args.dry_run and args.execute:
        ap.error("--dry-run 과 --execute 동시 사용 불가")

    db = Path(__file__).resolve().parents[1] / "purchase.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    targets = fetch_targets(conn)
    if args.limit:
        targets = targets[: args.limit]

    by_cat: dict[str, int] = {}
    for t in targets:
        by_cat[t["whole_name"]] = by_cat.get(t["whole_name"], 0) + 1

    logger.info(f"=== 대상 {len(targets)}건 ===")
    for cat, cnt in sorted(by_cat.items(), key=lambda x: -x[1]):
        logger.info(f"  {cnt:>4}  {cat}")

    if args.dry_run:
        logger.info("=== 샘플 (앞 10건) ===")
        for t in targets[:10]:
            title = (t["title_ko"] or "")[:40]
            logger.info(f"  origin={t['channel_product_id']:>12}  pid={t['product_id']:>5}  {t['whole_name']}  | {title}")
        logger.info(f"--- DRY-RUN 종료 (실행 안 됨) ---")
        return

    # --execute
    ok = 0
    skip_404 = 0
    fail = 0
    fail_msgs: list[tuple[str, str]] = []

    for i, t in enumerate(targets):
        origin_no = str(t["channel_product_id"])
        success, err = delete_product(origin_no)

        if success:
            ok += 1
            conn.execute(
                """UPDATE listings_pa
                   SET status='excluded',
                       error_message=?,
                       last_synced_at=datetime('now')
                   WHERE id=?""",
                (REASON, t["id"]),
            )
            conn.commit()
        elif "status=404" in err:
            # 404 는 그대로 둠 — DB 건드리지 않고 skip
            skip_404 += 1
        else:
            fail += 1
            if len(fail_msgs) < 30:
                fail_msgs.append((f"listing_id={t['id']} origin={origin_no}", err))

        if (i + 1) % 20 == 0 or (i + 1) == len(targets):
            logger.info(f"  progress {i+1}/{len(targets)} ok={ok} skip_404={skip_404} fail={fail}")

    logger.info(f"=== 완료: 삭제 성공 {ok}, 404 skip {skip_404}, 실패 {fail} ===")
    for label, err in fail_msgs:
        logger.warning(f"  {label}: {err}")


if __name__ == "__main__":
    main()
