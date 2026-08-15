"""
coupang_approval_from_api.py — 쿠팡 임시저장 상품 승인 요청 (API source-of-truth).

기존 coupang_approval.py 는 listings_pa 의 approval_requested_at NULL 만 대상으로 잡지만,
DB 동기화가 어긋나면 실제 쿠팡 임시저장 건수와 맞지 않는다 (현재 DB pending 52 vs 쿠팡 520).

이 스크립트는:
1. 쿠팡 API /seller-products?vendorId= 페이징으로 statusName='임시저장' 셀러상품 ID 전수 조회
2. 각 sellerProductId 에 대해 request_approval() 호출 (0.3s 간격)
3. 성공 시 listings_pa 의 매칭 row 가 있으면 approval_requested_at 업데이트 (sync)

사용:
    python3 -m backend.purchase.scripts.coupang_approval_from_api --dry-run
    python3 -m backend.purchase.scripts.coupang_approval_from_api --execute
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from backend.purchase.services.coupang_service import (
    BASE,
    COUPANG_VENDOR_ID,
    _request_with_retry,
    _signature,
    request_approval,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

INTERVAL = 0.3
TARGET_STATUS = "임시저장"


def fetch_temp_save_ids() -> list[int]:
    path = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
    next_token = ""
    out: list[int] = []
    page = 0
    t0 = time.time()
    while True:
        qs = f"vendorId={COUPANG_VENDOR_ID}&nextToken={next_token}&maxPerPage=100"
        r = _request_with_retry("GET", BASE + path + "?" + qs, headers=_signature("GET", path, qs), timeout=20)
        if r is None or r.status_code >= 400:
            raise RuntimeError(f"seller-products list 실패 page={page+1}: {r.status_code if r else 'None'}")
        body = r.json()
        for d in body.get("data") or []:
            if d.get("statusName") == TARGET_STATUS:
                out.append(int(d["sellerProductId"]))
        next_token = body.get("nextToken") or ""
        page += 1
        if page % 20 == 0:
            logger.info(f"  list page {page} (elapsed {time.time()-t0:.1f}s, found {len(out)})")
        if not next_token:
            break
        time.sleep(0.12)
    logger.info(f"임시저장 총 {len(out)}건 ({time.time()-t0:.1f}s, {page}페이지)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    if not args.dry_run and not args.execute:
        ap.error("--dry-run 또는 --execute 필요")
    if args.dry_run and args.execute:
        ap.error("동시 사용 불가")

    ids = fetch_temp_save_ids()
    if args.limit:
        ids = ids[: args.limit]

    if args.dry_run:
        logger.info(f"[DRY-RUN] 처리할 ID 개수: {len(ids)}, 앞 5: {ids[:5]}")
        return

    db = Path(__file__).resolve().parents[1] / "purchase.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    ok = 0
    fail = 0
    db_synced = 0
    fail_msgs: list[tuple[int, str]] = []

    for i, spid in enumerate(ids, start=1):
        success, err = request_approval(str(spid))
        if success:
            ok += 1
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            cur = conn.execute(
                """UPDATE listings_pa
                   SET approval_requested_at=?,
                       last_synced_at=CURRENT_TIMESTAMP,
                       error_message=NULL
                   WHERE channel='coupang' AND channel_product_id=?""",
                (now, str(spid)),
            )
            if cur.rowcount > 0:
                db_synced += 1
            conn.commit()
        else:
            fail += 1
            if len(fail_msgs) < 30:
                fail_msgs.append((spid, err))

        if i % 20 == 0 or i == len(ids):
            logger.info(f"  progress {i}/{len(ids)} ok={ok} fail={fail} db_synced={db_synced}")

        if i < len(ids):
            time.sleep(INTERVAL)

    logger.info(f"=== 완료: 승인요청 성공 {ok}, 실패 {fail}, DB sync {db_synced}/{ok} ===")
    for spid, err in fail_msgs:
        logger.warning(f"  spid={spid}: {err[:200]}")


if __name__ == "__main__":
    main()
