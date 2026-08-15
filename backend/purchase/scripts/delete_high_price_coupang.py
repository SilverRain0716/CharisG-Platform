"""
delete_high_price_coupang.py — 쿠팡 가격관리 엑셀에서 가격 임계치 이상 셀러상품 삭제.

운영 정책 (2026-05-04 사용자 결정):
  쿠팡 WING "가격관리" 다운로드 엑셀의 항목 중 my_price >= THRESHOLD 인
  vendorItem 들을 sellerProductId 단위로 grouping 한 뒤 delete_product (영구 삭제) 시도.
  쿠팡 정책상 임시저장/저장중 상태에서만 delete 가능 — 다른 상태는 실패할 수 있음.
  실패해도 DB 는 status='excluded' + error_message 로 마킹 (사용자 시스템에서 제외).

대상 파일 형식 (쿠팡 WING 가격관리 다운로드):
  row1: 안내 텍스트
  row2: 헤더 — 노출상품ID, 옵션ID, 상품명, 내판매가, ...
  row3+: 데이터

매칭 키: 엑셀 col "판매자 상품코드" (PA-{product_id}) → listings_pa.product_id.
        엑셀의 "노출상품ID" 는 쿠팡 productId(공개) 라서 sellerProductId 와 다름.
        externalVendorSku 는 coupang_lister 에서 'PA-{product_id}' 로 등록한다.

사용:
    python3 -m backend.purchase.scripts.delete_high_price_coupang \\
        --xlsx /tmp/coupang_price.xlsx --threshold 200000 --dry-run
    python3 -m backend.purchase.scripts.delete_high_price_coupang \\
        --xlsx /tmp/coupang_price.xlsx --threshold 200000 --execute
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

import openpyxl

from backend.purchase.services.coupang_service import delete_product

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

INTERVAL = 0.55  # 쿠팡 OPEN API rate limit 보호
PURCHASE_DB = Path(__file__).resolve().parents[1] / "purchase.db"


def parse_int(v) -> int | None:
    if v is None or v == "" or v == "-":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).replace(",", "").replace("원", "").strip()
    try:
        return int(s)
    except Exception:
        return None


def collect_targets(xlsx_path: Path, threshold: int) -> list[dict]:
    """엑셀에서 my_price >= threshold 필터링 후 PA-{product_id} 단위로 dedupe.

    매칭 키: col 18 (판매자 상품코드 = "PA-{product_id}").
    같은 product_id 의 여러 vendorItem 옵션은 최고가로 합산 (참조용).
    """
    wb = openpyxl.load_workbook(str(xlsx_path), read_only=True, data_only=True)
    ws = wb.active

    items: dict[int, dict] = {}
    for r in ws.iter_rows(min_row=3, values_only=True):
        if r[0] is None:
            continue
        my_price = parse_int(r[3])
        seller_code = str(r[18] or "").strip()
        if my_price is None or my_price < threshold or not seller_code.startswith("PA-"):
            continue
        try:
            product_id = int(seller_code.split("-", 1)[1])
        except (ValueError, IndexError):
            continue
        if product_id not in items:
            items[product_id] = {
                "product_id": product_id,
                "seller_code": seller_code,
                "exposed_id": str(r[0]).strip(),
                "name": r[2],
                "my_price": my_price,
            }
        else:
            if my_price > items[product_id]["my_price"]:
                items[product_id]["my_price"] = my_price
    return list(items.values())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", required=True, help="쿠팡 가격관리 엑셀 경로")
    ap.add_argument("--threshold", type=int, default=200000, help="가격 임계치 (원)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    if not args.dry_run and not args.execute:
        ap.error("--dry-run 또는 --execute 필요")
    if args.dry_run and args.execute:
        ap.error("동시 사용 불가")

    xlsx = Path(args.xlsx)
    if not xlsx.exists():
        logger.error(f"엑셀 없음: {xlsx}")
        return

    targets = collect_targets(xlsx, args.threshold)
    if args.limit:
        targets = targets[: args.limit]

    logger.info(
        f"가격 ≥ {args.threshold:,}원 셀러상품: {len(targets)}건 (product_id 기준 unique)"
    )

    # DB 매칭 — listings_pa.product_id 로 sellerProductId 조회
    conn = sqlite3.connect(str(PURCHASE_DB))
    conn.row_factory = sqlite3.Row

    matched = 0
    unmatched = 0
    for t in targets:
        row = conn.execute(
            """SELECT channel_product_id, status FROM listings_pa
               WHERE channel='coupang' AND product_id=?
               LIMIT 1""",
            (t["product_id"],),
        ).fetchone()
        if row and row["channel_product_id"]:
            matched += 1
            t["seller_product_id"] = row["channel_product_id"]
            t["db_status"] = row["status"]
        else:
            unmatched += 1
            t["seller_product_id"] = None
            t["db_status"] = row["status"] if row else None

    logger.info(f"  DB 매칭: {matched}건 / 매칭 실패 {unmatched}건")

    if args.dry_run:
        status_dist: dict[str, int] = {}
        for t in targets:
            k = t["db_status"] or "no_db_row"
            status_dist[k] = status_dist.get(k, 0) + 1
        logger.info(f"  DB status 분포: {status_dist}")
        logger.info("  샘플 10건:")
        for t in targets[:10]:
            logger.info(
                f"    pid={t['product_id']} spid={t['seller_product_id']} "
                f"db_status={t['db_status']} price={t['my_price']:,} {t['seller_code']}"
            )
        return

    # execute
    ok = 0
    fail = 0
    db_only = 0
    skipped = 0
    fail_msgs: list[tuple[str, str]] = []

    for i, t in enumerate(targets):
        spid = t.get("seller_product_id")
        if not spid:
            skipped += 1
            continue
        try:
            success, err = delete_product(str(spid))
        except Exception as e:
            success, err = False, f"예외: {e}"

        if success:
            ok += 1
        else:
            fail += 1
            if len(fail_msgs) < 10:
                fail_msgs.append((str(spid), err))

        new_msg = (
            "쿠팡 가격 임계치 초과 — 영구 삭제 성공"
            if success
            else f"쿠팡 가격 임계치 초과 — delete 실패 (DB만 정리): {err[:120]}"
        )
        conn.execute(
            """UPDATE listings_pa SET status='excluded',
               error_message=?,
               last_synced_at=datetime('now')
               WHERE channel='coupang' AND product_id=?""",
            (new_msg, t["product_id"]),
        )
        conn.commit()
        if not success:
            db_only += 1

        if (i + 1) % 50 == 0:
            logger.info(
                f"  progress {i+1}/{len(targets)} ok={ok} fail={fail} db_only={db_only}"
            )

        if i < len(targets) - 1:
            time.sleep(INTERVAL)

    logger.info(
        f"완료: 영구 삭제 성공 {ok}, 실패 {fail} (DB만 정리 {db_only}), 매칭 실패 스킵 {skipped}"
    )
    for spid, err in fail_msgs:
        logger.warning(f"  spid={spid}: {err[:200]}")


if __name__ == "__main__":
    main()
