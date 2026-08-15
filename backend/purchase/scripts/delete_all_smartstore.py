"""delete_all_smartstore.py — 스마트스토어 등록 상품 채널 삭제.

계정 정리(전량 철수) 용도. 채널에서 실제로 지우고 DB status 를 removed 로 맞춘다.

★되돌릴 수 없다. 실행 전 반드시 --dry-run 으로 대상을 확인할 것.

안전장치
--------
1. 진행 중 주문(취소·완료가 아닌 단계)이 걸린 상품은 자동 제외한다. 주문은 상품을
   지워도 이행해야 하는데, 상품이 없으면 상세·CS 처리가 꼬인다.
2. 삭제 성공한 건만 DB 를 removed 로 바꾼다. 실패는 error_message 에 남겨
   다음 실행에서 다시 시도되게 둔다(조용히 성공 처리하지 않는다).
3. 실행 전 대상 전체를 JSON 으로 백업한다(상품번호가 있어야 사후 추적이 된다).

사용:
    PYTHONPATH=<repo> .venv/bin/python -m backend.purchase.scripts.delete_all_smartstore \\
        --account old|new|both [--limit N] [--apply]
"""
import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))

import logging

from backend.purchase.database import get_db, get_db_hot
from backend.purchase.services.naver_commerce_service import naver_account, delete_product

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("ss-delete")

KST = timezone(timedelta(hours=9))
TERMINAL_STEPS = {"completed", "cancelled", "canceled", "delivered"}
SLEEP = 0.25          # 네이버 호출 간격 — 스로틀 응답은 서비스 계층이 따로 처리한다


def _live_order_pids() -> set[int]:
    """진행 중 주문이 걸린 product_id — 삭제 대상에서 뺀다."""
    out = set()
    with get_db_hot() as conn:
        for o in conn.execute(
            "SELECT product_id, current_step, canceled FROM orders WHERE channel='smartstore'"
        ):
            if o["product_id"] is None or o["canceled"]:
                continue
            if (o["current_step"] or "").lower() in TERMINAL_STEPS:
                continue
            out.add(o["product_id"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="both", choices=("old", "new", "both"))
    ap.add_argument("--limit", type=int, default=100000)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    accounts = ("old", "new") if args.account == "both" else (args.account,)
    skip_pids = _live_order_pids()

    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT id, product_id, channel_product_id pno, status,
                      COALESCE(NULLIF(naver_account,''),'old') acct
                 FROM listings_pa
                WHERE channel='smartstore' AND status IN ('listed','paused')
                  AND channel_product_id IS NOT NULL AND channel_product_id != ''
                ORDER BY id""").fetchall()]

    targets = [r for r in rows if r["acct"] in accounts][:args.limit]
    held = [r for r in targets if r["product_id"] in skip_pids]
    targets = [r for r in targets if r["product_id"] not in skip_pids]

    logger.info("삭제 대상 %d건 (진행 중 주문으로 보류 %d건)", len(targets), len(held))
    for h in held:
        logger.info("  보류: listing %s · pid %s", h["id"], h["product_id"])

    if not args.apply:
        by = {}
        for t in targets:
            by[t["acct"]] = by.get(t["acct"], 0) + 1
        logger.info("계정별: %s", by)
        logger.info("DRY-RUN — 실제 삭제하려면 --apply")
        return

    stamp = datetime.now(KST)
    backup = f"/home/ubuntu/charisg-deploy-backups/ss_delete_before_{stamp:%Y%m%d_%H%M%S}.json"
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    with open(backup, "w", encoding="utf-8") as f:
        json.dump(targets, f, ensure_ascii=False)
    logger.info("대상 백업: %s", backup)

    reason = f"계정 정리 — 채널 삭제 ({stamp:%Y-%m-%d})"
    ok = fail = 0
    for acct in accounts:
        mine = [t for t in targets if t["acct"] == acct]
        if not mine:
            continue
        logger.info("[%s] %d건 삭제 시작", acct, len(mine))
        with naver_account(acct):
            for i, t in enumerate(mine, 1):
                done, err = delete_product(str(t["pno"]))
                with get_db() as conn:
                    if done:
                        conn.execute(
                            "UPDATE listings_pa SET status='removed', error_message=?, "
                            "last_synced_at=datetime('now') WHERE id=?", (reason, t["id"]))
                        ok += 1
                    else:
                        conn.execute(
                            "UPDATE listings_pa SET error_message=? WHERE id=?",
                            (f"[삭제실패] {err[:180]}", t["id"]))
                        fail += 1
                        logger.warning("  실패 listing %s (상품 %s): %s", t["id"], t["pno"], err[:120])
                if i % 100 == 0:
                    logger.info("  [%s] %d/%d · 성공 %d 실패 %d", acct, i, len(mine), ok, fail)
                time.sleep(SLEEP)

    logger.info("완료 — 삭제 %d · 실패 %d · 보류 %d", ok, fail, len(held))


if __name__ == "__main__":
    main()
