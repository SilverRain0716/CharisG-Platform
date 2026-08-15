"""sync_coupang_option_ids.py — 쿠팡 옵션ID(vendorItemId) 일일 sync.

배경: 등록 직후엔 쿠팡이 vendorItemId 를 아직 부여 안 해 register_new_group_listing 의
_extract_coupang_option_ids 가 빈값 → listing_options.channel_option_id 에 NULL 저장됨.
승인/처리 후엔 GET seller-product 가 vendorItemId 를 돌려주므로, 이 잡이 매일 비어있는
옵션ID 를 채운다.

대상: channel='coupang' status='listed' 인 listing_options 중 channel_option_id 비어있는 것.
신규 우선(listing id DESC). per-run 상한으로 락 장기점유 방지(CSV-merge: GET 락밖, 주기 flush).
2,417 드리프트(쿠팡 vendorItem 0개)는 채워지지 않으나 reconcile-product-status 가 비listed 로
정리하면 대상에서 빠짐.

사용: python -m backend.purchase.scripts.sync_coupang_option_ids [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import time

from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))  # 단발 스크립트 env 명시 로드

import logging

from backend.purchase.database import get_db
from backend.purchase.services.coupang_service import coupang_account
from backend.purchase.services.group_lister import _extract_coupang_option_ids

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("cp-optid-sync")

PER_RUN_CAP = 3000   # per-run 처리 listing 상한 (락 장기점유 방지)
FLUSH_EVERY = 100    # N listing 마다 DB merge


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=PER_RUN_CAP)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with get_db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT l.id AS lid, l.channel_product_id AS spid,
                      COALESCE(NULLIF(l.coupang_account,''),'old') AS acct
               FROM listings_pa l JOIN listing_options o ON o.listing_id = l.id
               WHERE l.channel = 'coupang' AND l.status = 'listed'
                 AND l.channel_product_id IS NOT NULL
                 AND (o.channel_option_id IS NULL OR o.channel_option_id = '')
               ORDER BY l.id DESC
               LIMIT ?""",
            (args.limit,),
        ).fetchall()
    total = len(rows)
    logger.info("cp-optid-sync: 옵션ID 비어있는 listed listing %d건 (dry=%s)", total, args.dry_run)

    buf: list[tuple[str, int]] = []
    filled = 0
    getfail = 0
    matched = 0

    def flush() -> None:
        nonlocal buf, filled
        if not buf or args.dry_run:
            buf = []
            return
        with get_db() as conn:
            conn.executemany(
                "UPDATE listing_options SET channel_option_id=?, last_synced_at=datetime('now') WHERE id=?",
                buf,
            )
        filled += len(buf)
        buf = []

    for i, r in enumerate(rows, 1):
        spid = r["spid"]
        lid = r["lid"]
        try:
            # ★계정 컨텍스트(2026-08-08) — 종전엔 .env 기본계정(old)으로만 조회해
            #   신계정 리스팅 7,885건이 전부 GET 실패했다. 리스팅이 속한 계정으로 건다.
            with coupang_account(r["acct"]):
                m = _extract_coupang_option_ids(str(spid))  # {child_asin: vendorItemId}
        except Exception as e:  # noqa: BLE001
            getfail += 1
            logger.warning("GET 실패 spid=%s: %s", spid, str(e)[:120])
            m = None
        if m:
            with get_db() as conn:
                opts = conn.execute(
                    """SELECT o.id, p.asin FROM listing_options o
                       JOIN products p ON p.id = o.child_product_id
                       WHERE o.listing_id = ?
                         AND (o.channel_option_id IS NULL OR o.channel_option_id = '')""",
                    (lid,),
                ).fetchall()
            for o in opts:
                vid = m.get(o["asin"])
                if vid:
                    buf.append((str(vid), o["id"]))
                    matched += 1
        else:
            getfail += 1
        if i % FLUSH_EVERY == 0:
            flush()
            logger.info("진행 %d/%d  매칭 %d  채움 %d  GET실패 %d", i, total, matched, filled, getfail)
        time.sleep(0.08)  # SP/coupang rate 완화

    flush()
    logger.info("=== cp-optid-sync 완료: listing %d  채운옵션 %d  매칭 %d  GET실패(또는 vendorItem 0) %d ===",
                total, filled, matched, getfail)


if __name__ == "__main__":
    main()
