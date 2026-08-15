"""
coupang_price_lower_to_recommended.py — 119 SKU 쿠팡 추천가 일괄 적용.

사용자 합의 정책:
  쿠팡 추천가 적용 + 발급된 5쿠폰 적용 후 마진 ≥15,000원 유지 SKU 만 대상.

흐름:
  1. listings_pa.cost_krw_snapshot + 추천가 입력 csv 읽기
  2. 백업 — 현재 sale_krw, net_margin_krw 를 별도 csv 로 저장 (롤백 대비)
  3. 각 vendor_item_id 에 대해 update_vendor_item_price 호출 (rate limit 0.3s)
  4. 성공 시 listings_pa.sale_krw 업데이트 + net_margin_krw 재계산

CSV 입력: /tmp/price_update_targets.csv
  vendor_item_id,recommended_price
"""
import argparse
import csv
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from backend.purchase.services.coupang_service import update_vendor_item_price

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

FEE = 0.13
TAX = 0.10
INTERVAL = 0.3


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="vendor_item_id,recommended_price")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--backup", default="/tmp/price_update_backup.csv")
    args = ap.parse_args()

    if not args.dry_run and not args.execute:
        ap.error("--dry-run 또는 --execute 필요")

    # CSV 입력 읽기
    targets = []
    with open(args.csv) as f:
        for r in csv.DictReader(f):
            targets.append({"vid": str(r["vendor_item_id"]), "new_price": int(r["recommended_price"])})
    logger.info(f"대상: {len(targets)}건")

    db = Path(__file__).resolve().parents[1] / "purchase.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    # 백업: 현재 sale_krw / net_margin_krw / cost
    vids = [t["vid"] for t in targets]
    ph = ",".join("?" * len(vids))
    rows = conn.execute(
        f"""SELECT ci.vendor_item_id, ci.listing_id, l.id AS lid, l.product_id,
                   l.sale_krw, l.net_margin_krw, l.cost_krw_snapshot
            FROM coupon_items ci
            JOIN listings_pa l ON l.id = ci.listing_id
            WHERE ci.vendor_item_id IN ({ph})""",
        vids,
    ).fetchall()
    info = {str(r["vendor_item_id"]): dict(r) for r in rows}
    logger.info(f"DB 매칭: {len(info)}/{len(targets)}")

    # 백업 csv
    with open(args.backup, "w", newline="") as bf:
        w = csv.writer(bf)
        w.writerow(["vendor_item_id", "listing_id", "old_sale_krw", "old_net_margin_krw", "cost_krw_snapshot"])
        for t in targets:
            d = info.get(t["vid"])
            if d:
                w.writerow([t["vid"], d["lid"], d["sale_krw"], d["net_margin_krw"], d["cost_krw_snapshot"]])
    logger.info(f"백업 저장: {args.backup}")

    if args.dry_run:
        for t in targets[:5]:
            d = info.get(t["vid"], {})
            logger.info(f"  [DRY] vid={t['vid']} {d.get('sale_krw')} → {t['new_price']} (cost {d.get('cost_krw_snapshot')})")
        logger.info(f"--- DRY-RUN 종료 ---")
        return

    # 실제 업데이트
    ok = 0
    fail = 0
    fail_msgs = []
    for i, t in enumerate(targets, 1):
        d = info.get(t["vid"])
        if not d:
            fail += 1
            fail_msgs.append((t["vid"], "DB 매칭 실패"))
            continue
        success, err = update_vendor_item_price(t["vid"], t["new_price"])
        if success:
            ok += 1
            # 신 마진 재계산
            new_margin = int(t["new_price"] - d["cost_krw_snapshot"] - d["cost_krw_snapshot"] * TAX - t["new_price"] * FEE)
            new_pct = round(new_margin / t["new_price"] * 100, 1)
            conn.execute(
                """UPDATE listings_pa
                   SET sale_krw=?, net_margin_krw=?, net_margin_pct=?, last_synced_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (t["new_price"], new_margin, new_pct, d["lid"]),
            )
            conn.commit()
        else:
            fail += 1
            if len(fail_msgs) < 30:
                fail_msgs.append((t["vid"], err))
        if i % 10 == 0 or i == len(targets):
            logger.info(f"  progress {i}/{len(targets)} ok={ok} fail={fail}")
        if i < len(targets):
            time.sleep(INTERVAL)

    logger.info(f"=== 완료: ok={ok} fail={fail} ===")
    for vid, err in fail_msgs:
        logger.warning(f"  vid={vid}: {err[:200]}")


if __name__ == "__main__":
    main()
