"""reconcile_coupang_old_status.py — 쿠팡 구계정 DB 상태를 채널 실제와 맞춘다.

배경
----
구계정(카리스G)은 상품을 전량 삭제했는데 DB 에는 listed/paused 로 남아 있었다.
목록 API(seller-products)는 삭제된 계정에 대해 0건을 돌려주고, 개별 조회는
`statusName='상품삭제'` 인 기록을 돌려준다 — 응답이 왔다고 살아있는 게 아니다.

★2026-08-10 조사 중 이 지점에서 판단을 한 번 그르쳤다. get_seller_product 가
  200 을 주는 것만 보고 '살아있음'으로 셌는데, 실제로는 삭제 기록이었다.
  상태 판정은 반드시 statusName 을 읽어서 한다.

목록 API 를 믿지 않고 건별로 조회한다(수백 건 규모라 감당 가능).
"""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))

import logging

from backend.purchase.database import get_db
from backend.purchase.services import coupang_service as CS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("cp-old-reconcile")

KST = timezone(timedelta(hours=9))
DELETED = {"상품삭제"}


def _status_of(spid: str) -> str | None:
    """채널이 보는 상태. 조회 실패는 None (모름) — 삭제로 단정하지 않는다."""
    d = CS.get_seller_product(str(spid))
    if not d:
        return None
    # get_seller_product 가 봉투째 줄 수도, data 만 줄 수도 있다. 둘 다 받는다.
    body = d.get("data") if isinstance(d.get("data"), dict) else d
    return body.get("statusName")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="old")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=5000)
    args = ap.parse_args()

    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT id, channel_product_id spid, status
                 FROM listings_pa
                WHERE channel='coupang'
                  AND COALESCE(NULLIF(coupang_account,''),'old')=?
                  AND status IN ('listed','paused')
                  AND channel_product_id IS NOT NULL AND channel_product_id != ''
                ORDER BY id LIMIT ?""", (args.account, args.limit)).fetchall()]
    logger.info("대상 %d건", len(rows))

    tally: dict[str, int] = {}
    to_remove: list[int] = []
    unknown: list[int] = []
    with CS.coupang_account(args.account):
        for i, r in enumerate(rows, 1):
            st = _status_of(r["spid"])
            key = st or "(조회실패)"
            tally[key] = tally.get(key, 0) + 1
            if st in DELETED:
                to_remove.append(r["id"])
            elif st is None:
                unknown.append(r["id"])
            if i % 50 == 0:
                logger.info("  %d/%d · %s", i, len(rows), tally)

    logger.info("채널 상태 분포: %s", tally)
    logger.info("삭제로 정정할 건: %d · 조회실패(보류): %d", len(to_remove), len(unknown))

    if not args.apply:
        logger.info("DRY-RUN — 적용하려면 --apply")
        return
    if not to_remove:
        logger.info("정정할 것 없음")
        return

    stamp = datetime.now(KST)
    backup = f"/home/ubuntu/charisg-deploy-backups/cp_old_status_before_{stamp:%Y%m%d_%H%M%S}.json"
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    with open(backup, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    logger.info("변경 전 백업: %s", backup)

    reason = f"채널 statusName='상품삭제' 확인 ({stamp:%Y-%m-%d})"
    with get_db() as conn:
        for i in range(0, len(to_remove), 500):
            chunk = to_remove[i:i + 500]
            conn.execute(
                "UPDATE listings_pa SET status='removed', error_message=?, "
                "last_synced_at=datetime('now') WHERE id IN (%s)"
                % ",".join("?" * len(chunk)),
                (reason, *chunk))
    logger.info("적용 완료 — %d건 removed", len(to_remove))


if __name__ == "__main__":
    main()
