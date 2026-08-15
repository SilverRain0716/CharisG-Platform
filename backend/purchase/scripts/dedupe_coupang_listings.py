"""
dedupe_coupang_listings.py — 쿠팡 중복 listing 정리 (판매중지).

중복 판정: 동일 products.asin 이 listings_pa 에 coupang/listed 상태로 여러 건.
각 ASIN 그룹에서 **가장 먼저 등록된 것(products.id 최소) 하나만 유지**,
나머지는 stop_sales (판매 중지) + DB excluded 처리.

운영 정책 (2026-05-04 사용자 결정):
  - 처음엔 delete_product 시도했으나 쿠팡 API 제약 발견:
    "삭제는 '저장중', '임시저장' 상태에서만 가능합니다."
    → 승인완료된 셀러상품은 직접 삭제 불가
  - 대안: stop_sales (vendorItem 단위 판매중지) — 네이버 SUSPENSION 과 동일 사상
    클린프로그램 위반 해소 효과는 동일, 매출 영향 0 (주문 0건 listing 만 대상)
  - 주문 이력 있는 listing 은 자동 제외 (judg by hot.orders)

사용:
    python3 -m backend.purchase.scripts.dedupe_coupang_listings --dry-run [--limit N]
    python3 -m backend.purchase.scripts.dedupe_coupang_listings --execute [--limit N]
"""
import argparse
import logging
import sqlite3
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from backend.purchase.services.coupang_service import stop_sales

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# 쿠팡 OPEN API rate limit 보호 (호출 간 간격)
INTERVAL = 0.55

PURCHASE_DB = Path(__file__).resolve().parents[1] / "purchase.db"
PURCHASE_HOT_DB = Path(__file__).resolve().parents[1] / "purchase_hot.db"


def collect_duplicates(conn) -> list[dict]:
    """주문 0건인 ASIN 중복 그룹에서 keep + remove 페어 추출.

    각 그룹에서 product.id 최소값을 keep, 나머지를 remove.
    그룹 멤버 중 주문이 1건이라도 있으면 그 멤버는 건드리지 않고,
    주문 없는 멤버만 remove 후보 (단, keep 도 주문 없는 것 중 최소 id).

    그룹 전체 멤버가 모두 주문 0이면 — 가장 오래된 1건 keep, 나머지 delete.
    """
    rows = conn.execute(
        """SELECT p.asin, l.product_id, l.channel_product_id,
                  COALESCE(
                    (SELECT COUNT(*) FROM hot.orders o
                     WHERE o.channel='coupang' AND o.product_id=l.product_id),
                    0
                  ) AS order_cnt
           FROM listings_pa l JOIN products p ON p.id=l.product_id
           WHERE l.channel='coupang' AND l.status='listed'
             AND p.asin IS NOT NULL AND p.asin != ''
           ORDER BY p.asin, p.id"""
    ).fetchall()

    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["asin"], []).append({
            "product_id": r["product_id"],
            "channel_product_id": r["channel_product_id"],
            "order_cnt": r["order_cnt"],
        })

    targets = []
    for asin, items in groups.items():
        if len(items) <= 1:
            continue

        # 주문 0인 멤버만 후보 (판매된 listing 은 보존)
        zero_items = [m for m in items if m["order_cnt"] == 0]
        sold_items = [m for m in items if m["order_cnt"] > 0]

        if not zero_items:
            # 모든 멤버가 주문 있음 — 건드리지 않음
            continue

        # keep 결정
        if sold_items:
            # 주문 있는 listing 중 첫 번째를 keep — 0인 것들은 모두 delete
            keep = sold_items[0]
            removes = zero_items
        else:
            # 모두 0건 — 가장 오래된 1건 keep, 나머지 delete
            keep = zero_items[0]
            removes = zero_items[1:]

        for dup in removes:
            targets.append({
                "asin": asin,
                "keep_pid": keep["product_id"],
                "remove_pid": dup["product_id"],
                "remove_cpid": dup["channel_product_id"],
            })
    return targets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    if not args.dry_run and not args.execute:
        ap.error("--dry-run 또는 --execute 필요")
    if args.dry_run and args.execute:
        ap.error("동시 사용 불가")

    conn = sqlite3.connect(str(PURCHASE_DB))
    conn.row_factory = sqlite3.Row
    conn.execute(f"ATTACH DATABASE '{PURCHASE_HOT_DB}' AS hot")

    targets = collect_duplicates(conn)
    if args.limit:
        targets = targets[: args.limit]
    logger.info(
        f"쿠팡 중복 제거 대상: {len(targets)}건 "
        f"(주문 0건 listing 만, 각 ASIN 그룹에서 1건은 keep)"
    )

    if args.dry_run:
        for t in targets[:10]:
            logger.info(
                f"  [DRY] asin={t['asin']} keep pid={t['keep_pid']} "
                f"remove pid={t['remove_pid']} cpid={t['remove_cpid']}"
            )
        if len(targets) > 10:
            logger.info(f"  ... (총 {len(targets)}건)")
        return

    ok = 0
    fail = 0
    fail_msgs: list[tuple[str, str]] = []

    for i, t in enumerate(targets):
        cpid = t["remove_cpid"]
        if not cpid:
            continue

        try:
            success, err = stop_sales(str(cpid))
        except Exception as e:
            success, err = False, f"예외: {e}"

        if success:
            ok += 1
            # DB 업데이트 — 판매중지로 전환, DB 는 excluded 처리
            conn.execute(
                """UPDATE listings_pa SET status='excluded',
                   error_message='쿠팡 중복 ASIN 정리 — 판매중지 (주문 0건)',
                   last_synced_at=datetime('now')
                   WHERE channel='coupang' AND product_id=?""",
                (t["remove_pid"],),
            )
            conn.commit()
        else:
            fail += 1
            if len(fail_msgs) < 10:
                fail_msgs.append((str(cpid), err))

        if (i + 1) % 50 == 0:
            logger.info(f"  progress {i+1}/{len(targets)} ok={ok} fail={fail}")

        # rate limit
        if i < len(targets) - 1:
            time.sleep(INTERVAL)

    logger.info(f"완료: 판매중지 {ok}, 실패 {fail}")
    for cpid, err in fail_msgs:
        logger.warning(f"  cpid={cpid}: {err[:200]}")


if __name__ == "__main__":
    main()
