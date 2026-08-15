"""sync_smartstore_option_ids.py — 스마트스토어 옵션ID 백필/동기화.

옵션이 있는 리스팅은 주문이 들어왔을 때 `listing_options.channel_option_id` 로
어느 옵션이 팔렸는지 특정한다. 이 값이 비면 형제 상품으로 폴백해 **다른 맛·다른
용량이 나간다**(오배송).

쿠팡에는 같은 일을 하는 sync_coupang_option_ids.py 가 야간에 돌고 있었는데
스마트스토어에는 대응 잡이 없었다. 그 결과 구계정(카리스G) 리스팅은 옵션ID가
100% 비어 있었다 — 등록 시점에 기록하는 로직이 들어오기 전에 올라간 것들이라
가만히 두면 영원히 안 채워진다.

대상: channel='smartstore' AND status IN ('listed','paused') 인 listing_options 중
      channel_option_id 가 비어 있는 것.

사용:
    PYTHONPATH=<repo> .venv/bin/python -m backend.purchase.scripts.sync_smartstore_option_ids \\
        [--account old|new|both] [--limit N] [--dry-run]

★계정마다 자격증명이 다르므로 naver_account() 컨텍스트 안에서 조회한다.
  계정을 안 씌우면 활성 계정(NAVER_ACTIVE)으로 조회해 엉뚱한 스토어를 본다.
"""
import argparse
import os
import sys

from dotenv import load_dotenv

_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))   # 단발 스크립트 env 명시 로드

import logging

from backend.purchase.database import get_db
from backend.purchase.services.naver_commerce_service import naver_account
from backend.purchase.services.group_lister import _extract_smartstore_option_ids

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("ss-optid-sync")

PER_RUN_CAP = 1000   # per-run 상한 — 락 장기점유 방지
FLUSH_EVERY = 20     # N listing 마다 DB 반영


def _targets(account: str, limit: int):
    """옵션ID가 빈 리스팅 목록. 계정별로 나눠 조회한다."""
    with get_db() as conn:
        return conn.execute(
            """SELECT DISTINCT l.id AS lid, l.channel_product_id AS pno, l.status
                 FROM listings_pa l
                 JOIN listing_options o ON o.listing_id = l.id
                WHERE l.channel = 'smartstore'
                  AND l.status IN ('listed', 'paused')
                  AND COALESCE(NULLIF(l.naver_account, ''), 'old') = ?
                  AND l.channel_product_id IS NOT NULL AND l.channel_product_id != ''
                  AND (o.channel_option_id IS NULL OR o.channel_option_id = '')
                ORDER BY l.id
                LIMIT ?""",
            (account, limit),
        ).fetchall()


def _run_account(account: str, limit: int, dry: bool) -> dict:
    rows = _targets(account, limit)
    logger.info("[%s] 대상 리스팅 %d건", account, len(rows))
    if not rows:
        return {"listings": 0, "filled": 0, "getfail": 0, "unmatched": 0}

    filled = getfail = unmatched = 0
    pending: list[tuple[str, int]] = []

    def flush():
        nonlocal pending
        if not pending or dry:
            pending = []
            return
        with get_db() as conn:
            conn.executemany(
                "UPDATE listing_options SET channel_option_id=?, "
                "last_synced_at=datetime('now') WHERE id=?",
                pending,
            )
        pending = []

    with naver_account(account):
        for i, r in enumerate(rows, 1):
            id_map = _extract_smartstore_option_ids(r["pno"])
            if not id_map:
                getfail += 1
                logger.warning("  listing %s (상품 %s): 채널 조회 실패 또는 옵션 없음",
                               r["lid"], r["pno"])
                continue

            with get_db() as conn:
                opts = conn.execute(
                    """SELECT o.id, p.asin FROM listing_options o
                         JOIN products p ON p.id = o.child_product_id
                        WHERE o.listing_id = ?
                          AND (o.channel_option_id IS NULL OR o.channel_option_id = '')""",
                    (r["lid"],),
                ).fetchall()

            for o in opts:
                cid = id_map.get(o["asin"])
                if cid:
                    pending.append((str(cid), o["id"]))
                    filled += 1
                else:
                    unmatched += 1
                    logger.warning("  listing %s: ASIN %s 가 채널 옵션에 없음",
                                   r["lid"], o["asin"])

            if i % FLUSH_EVERY == 0:
                flush()
                logger.info("  ... %d/%d", i, len(rows))

    flush()
    return {"listings": len(rows), "filled": filled,
            "getfail": getfail, "unmatched": unmatched}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="both", choices=("old", "new", "both"))
    ap.add_argument("--limit", type=int, default=PER_RUN_CAP)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    accounts = ("old", "new") if args.account == "both" else (args.account,)
    total = {"listings": 0, "filled": 0, "getfail": 0, "unmatched": 0}
    for acct in accounts:
        res = _run_account(acct, args.limit, args.dry_run)
        for k in total:
            total[k] += res[k]

    logger.info("%s 리스팅 %d · 채움 %d · 조회실패 %d · 미매칭 %d",
                "[DRY-RUN] " if args.dry_run else "완료:",
                total["listings"], total["filled"], total["getfail"], total["unmatched"])
    # 미매칭이 남으면 사람이 봐야 한다 — 조용히 성공으로 끝내지 않는다.
    sys.exit(1 if total["unmatched"] or total["getfail"] else 0)


if __name__ == "__main__":
    main()
