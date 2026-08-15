"""
coupang_publish_coupon_policy.py — 마진대 기반 7쿠폰 일괄 발급.

정책 (2026-05-14 갱신):
  margin <  10K       : 미적용
  margin 10K-15K      : PRICE 1,000원   / cap 10,000
  margin 15K-50K      : RATE 5%         / cap 10,000
  margin 50K-70K      : PRICE 5,000원   / cap 10,000
  margin 70K-100K     : PRICE 10,000원  / cap 20,000
  margin 100K-150K    : PRICE 20,000원  / cap 30,000
  margin 150K-200K    : PRICE 30,000원  / cap 50,000
  margin 200K+        : PRICE 50,000원  / cap 70,000

흐름 (--no-apply 면 1~2 발행만, listings 처리는 apply_coupon_catchup 로 분리):
  1) 7개 쿠폰 순차 발급:
     create_coupon → reqId → wait DONE → couponId 회수 + coupons INSERT
  2) (default) listings_pa 마진대 분류 + vendorItemId 추출 + add_coupon_items
  3) (--no-apply) skip — apply_coupon_catchup 별도 호출

사용:
  python3 -m backend.purchase.scripts.coupang_publish_coupon_policy --dry-run
  python3 -m backend.purchase.scripts.coupang_publish_coupon_policy --execute
  python3 -m backend.purchase.scripts.coupang_publish_coupon_policy --execute --no-apply  # 발행만
"""
import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from backend.purchase.services.coupang_service import (
    add_coupon_items,
    create_coupon,
    discover_contract_id,
    get_vendor_item_ids,
    wait_for_request,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CHUNK = 10_000     # add_coupon_items 1회 한도
ADD_INTERVAL = 0.3  # 청크 간 대기

# (margin_band, name_suffix, type, discount, max_discount_price, lo, hi)
# 2026-05-14 정책: 7단계 — 70K+ 부터 정률 5% → 정액으로 변경
# margin_band 는 coupon_apply._margin_band 의 출력과 정확히 일치해야 함 (활성 쿠폰 동적 조회 키)
COUPONS = [
    ("10-15K",   "마진10-15K_정액1000",   "PRICE",  1000, 10000,  10000,  15000),
    ("15-50K",   "마진15-50K_5pct_cap10K", "RATE",     5, 10000,  15000,  50000),
    ("50-70K",   "마진50-70K_정액5000",   "PRICE",  5000, 10000,  50000,  70000),
    ("70-100K",  "마진70-100K_정액10000", "PRICE", 10000, 20000,  70000, 100000),
    ("100-150K", "마진100-150K_정액20000","PRICE", 20000, 30000, 100000, 150000),
    ("150-200K", "마진150-200K_정액30000","PRICE", 30000, 50000, 150000, 200000),
    ("200K+",    "마진200K+_정액50000",   "PRICE", 50000, 70000, 200000,  10**12),
]


def fetch_listings_in_margin_range(conn: sqlite3.Connection, lo: int, hi: int) -> list[dict]:
    """채널=coupang, status=listed, margin in [lo, hi) 인 listings_pa rows."""
    cur = conn.execute(
        """SELECT id, product_id, channel_product_id, sale_krw, net_margin_krw
           FROM listings_pa
           WHERE channel='coupang' AND status='listed'
             AND channel_product_id IS NOT NULL AND channel_product_id != ''
             AND net_margin_krw IS NOT NULL
             AND net_margin_krw >= ? AND net_margin_krw < ?""",
        (lo, hi),
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def expand_to_vendor_items(listings: list[dict], dry_run: bool = False) -> list[dict]:
    """seller_product_id → vendorItemId N개 (multi-option). [{listing_id, spid, vid}...]."""
    out = []
    for i, lst in enumerate(listings, 1):
        spid = str(lst["channel_product_id"])
        if dry_run:
            # dry-run 에선 SP-API 안 부르고 spid 1:1 가정
            out.append({"listing_id": lst["id"], "spid": spid, "vendor_item_id": None})
            continue
        try:
            vids = get_vendor_item_ids(spid)
        except Exception as e:
            logger.warning(f"  vendorItemIds 조회 실패: spid={spid} {e}")
            continue
        for vid in vids:
            out.append({"listing_id": lst["id"], "spid": spid, "vendor_item_id": int(vid)})
        if i % 50 == 0:
            logger.info(f"    vendorItemIds 추출 {i}/{len(listings)} 누적 vid={len(out)}")
        time.sleep(0.1)  # rate limit 보호
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--start-at", type=str, help="yyyy-MM-dd HH:mm:ss (default: 다음날 00:00)")
    ap.add_argument("--end-at", type=str, help="yyyy-MM-dd HH:mm:ss (default: start_at + days)")
    ap.add_argument("--days", type=int, default=30, help="유효기간 일수 (--end-at 미지정시)")
    ap.add_argument("--contract-id", type=int, help="contractId 명시 (default: 자동 발견)")
    ap.add_argument("--limit-per-coupon", type=int, default=None, help="테스트용: 쿠폰당 최대 listing 수")
    ap.add_argument("--no-apply", action="store_true",
                    help="쿠폰 발행만 — listings 추가는 apply_coupon_catchup 로 별도 실행")
    ap.add_argument("--month", action="store_true",
                    help="이번 달 1일 00:00 ~ 말일 23:59 KST 자동 설정 (월간 타이머용, bash 날짜계산 제거)")
    args = ap.parse_args()

    if not args.dry_run and not args.execute:
        ap.error("--dry-run 또는 --execute 필수")
    if args.dry_run and args.execute:
        ap.error("동시 사용 불가")

    # 일정 결정
    if args.month:
        # ★유닛파일 bash date 계산(escape 버그)을 대체 — 이번 달 1일~말일 KST
        kst_now = datetime.now(timezone(timedelta(hours=9)))
        start_dt = kst_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        nxt = (start_dt.replace(year=start_dt.year + 1, month=1)
               if start_dt.month == 12 else start_dt.replace(month=start_dt.month + 1))
        end_dt = nxt - timedelta(minutes=1)  # 말일 23:59
        start_at = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        end_at = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    elif args.start_at:
        start_at = args.start_at
    else:
        # 한국시간 기준 다음날 00:00
        kst_now = datetime.now(timezone(timedelta(hours=9)))
        next_day = (kst_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        start_at = next_day.strftime("%Y-%m-%d %H:%M:%S")
    if args.end_at:
        end_at = args.end_at
    elif not args.month:
        end_at_dt = datetime.strptime(start_at, "%Y-%m-%d %H:%M:%S") + timedelta(days=args.days)
        end_at_dt = end_at_dt - timedelta(minutes=1)  # 23:59
        end_at = end_at_dt.strftime("%Y-%m-%d %H:%M:%S")

    logger.info(f"기간: {start_at} ~ {end_at} ({args.days}일)")

    # contractId
    contract_id = args.contract_id or discover_contract_id()
    if not contract_id:
        logger.error("contractId 발견 실패. --contract-id 명시 필요")
        sys.exit(1)
    logger.info(f"contractId: {contract_id}")

    db = Path(__file__).resolve().parents[1] / "purchase.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    # ─ 사전 plan 출력 ─
    plans = []
    total_listings = 0
    for band, suffix, t_, disc, cap, lo, hi in COUPONS:
        if args.no_apply:
            listings = []  # 발행만 — listings fetch skip
        else:
            listings = fetch_listings_in_margin_range(conn, lo, hi)
            if args.limit_per_coupon:
                listings = listings[: args.limit_per_coupon]
        total_listings += len(listings)
        plans.append({
            "band": band, "suffix": suffix, "type": t_,
            "discount": disc, "cap": cap, "listings": listings,
        })
        logger.info(f"  [{band:>10}] {t_:>5} {disc} cap={cap:,} listings={len(listings):>5}")

    logger.info(f"=== 총 listings: {total_listings} ({'no-apply 모드' if args.no_apply else 'apply 모드'}) ===")

    if args.dry_run:
        logger.info("--- DRY-RUN: 실제 발급 안 함 ---")
        # vendorItemId 추출은 비싸니 dry-run 에선 건너뛴다
        for p in plans:
            logger.info(f"  [{p['band']}] 첫 5건 listing_id: {[l['id'] for l in p['listings'][:5]]}")
        return

    # ─ 실 발급 ─
    for p in plans:
        if not p["listings"] and not args.no_apply:
            logger.info(f"  [{p['band']}] 대상 없음 — skip")
            continue

        # YYMM 은 start_at 기준 (매월 1일 발행 시 그 달 라벨)
        ym_label = datetime.strptime(start_at, "%Y-%m-%d %H:%M:%S").strftime("%y%m")
        coupon_name = f"CharisG_{p['suffix']}_{ym_label}"
        logger.info(f"=== [{p['band']}] 발급 시작: name={coupon_name} ===")

        # 1) create_coupon
        ok, err, req_id = create_coupon(
            contract_id=contract_id,
            name=coupon_name,
            discount=p["discount"],
            max_discount_price=p["cap"],
            start_at=start_at,
            end_at=end_at,
            type_=p["type"],
        )
        if not ok:
            logger.error(f"  생성 실패: {err}")
            continue
        logger.info(f"  create_coupon ok, reqId={req_id}")

        result = wait_for_request(req_id, timeout=120, interval=2)
        if not result or result.get("status") != "DONE":
            logger.error(f"  생성 polling FAIL: {result}")
            # coupons 테이블에 failed 기록
            conn.execute(
                """INSERT INTO coupons (contract_id, name, type, discount, max_discount_price,
                                        start_at, end_at, status, requested_id, margin_band,
                                        target_count, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?, ?)""",
                (contract_id, coupon_name, p["type"], p["discount"], p["cap"],
                 start_at, end_at, req_id, p["band"],
                 len(p["listings"]), str(result)[:300]),
            )
            conn.commit()
            continue

        coupon_id = result.get("couponId")
        logger.info(f"  ✓ couponId={coupon_id}")

        # coupons INSERT
        cur = conn.execute(
            """INSERT INTO coupons (coupon_id, contract_id, name, type, discount, max_discount_price,
                                    start_at, end_at, status, requested_id, margin_band, target_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
            (coupon_id, contract_id, coupon_name, p["type"], p["discount"], p["cap"],
             start_at, end_at, req_id, p["band"], len(p["listings"])),
        )
        conn.commit()
        coupon_local_id = cur.lastrowid

        if args.no_apply:
            logger.info(f"  [{p['band']}] --no-apply: listings 추가 skip (catchup 별도)")
            continue

        # 2) vendorItemId 추출
        logger.info(f"  vendorItemId 추출 시작 ({len(p['listings'])} listings)")
        vis = expand_to_vendor_items(p["listings"], dry_run=False)
        logger.info(f"  vendorItem 총 {len(vis)}개 추출 완료")

        # coupon_items pre-INSERT (pending)
        for v in vis:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO coupon_items
                       (coupon_local_id, coupon_id, vendor_item_id, seller_product_id, listing_id,
                        status)
                       VALUES (?, ?, ?, ?, ?, 'pending')""",
                    (coupon_local_id, coupon_id, v["vendor_item_id"], int(v["spid"]),
                     v["listing_id"]),
                )
            except Exception:
                pass
        conn.commit()

        # 3) 청크별 add_coupon_items
        vid_list = [v["vendor_item_id"] for v in vis if v["vendor_item_id"]]
        items_ok = 0
        items_fail = 0
        for ci in range(0, len(vid_list), CHUNK):
            chunk = vid_list[ci : ci + CHUNK]
            chunk_idx = ci // CHUNK
            logger.info(f"    청크 {chunk_idx + 1} ({len(chunk)}건) 추가 중…")
            ok, err, req_id2 = add_coupon_items(coupon_id, chunk)
            if not ok:
                logger.error(f"    chunk add 실패: {err}")
                items_fail += len(chunk)
                continue
            res2 = wait_for_request(req_id2, timeout=300, interval=3)
            if not res2:
                logger.error(f"    polling timeout")
                items_fail += len(chunk)
                continue
            succeeded = res2.get("succeeded") or 0
            failed = res2.get("failed") or 0
            items_ok += succeeded
            items_fail += failed
            logger.info(f"    chunk {chunk_idx + 1} status={res2.get('status')} ok={succeeded} fail={failed}")

            # 결과 반영 — 성공한 vid
            failed_set = {(f.get("vendorItemId"), f.get("reason")) for f in (res2.get("failedVendorItems") or [])}
            failed_vids = {fv[0] for fv in failed_set}
            for v in chunk:
                if v in failed_vids:
                    reason = next((rs for vid, rs in failed_set if vid == v), "unknown")
                    conn.execute(
                        "UPDATE coupon_items SET status='failed', fail_reason=?, requested_id=?, chunk_index=? WHERE coupon_local_id=? AND vendor_item_id=?",
                        (reason[:200], req_id2, chunk_idx, coupon_local_id, v),
                    )
                else:
                    conn.execute(
                        "UPDATE coupon_items SET status='applied', requested_id=?, chunk_index=? WHERE coupon_local_id=? AND vendor_item_id=?",
                        (req_id2, chunk_idx, coupon_local_id, v),
                    )
            conn.commit()
            time.sleep(ADD_INTERVAL)

        # 쿠폰 row 업데이트
        conn.execute(
            "UPDATE coupons SET items_added=?, items_failed=?, last_synced_at=CURRENT_TIMESTAMP WHERE id=?",
            (items_ok, items_fail, coupon_local_id),
        )
        conn.commit()

        logger.info(f"=== [{p['band']}] 완료: couponId={coupon_id} 추가성공={items_ok} 실패={items_fail} ===")
        time.sleep(1)

    logger.info("=== 모든 쿠폰 발급 완료 ===")


if __name__ == "__main__":
    main()
