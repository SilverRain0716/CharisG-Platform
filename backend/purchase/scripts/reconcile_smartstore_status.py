"""reconcile_smartstore_status.py — 스마트스토어 DB 상태를 채널 실제와 맞춘다.

배경
----
`listings_pa.status='listed'` 는 등록 시점에 쓰이고, 그 뒤 채널에서 삭제·중지되어도
누가 알려주지 않는다. 그래서 DB 의 listed 가 채널 실제보다 부풀어 오른다.
실측(2026-08-10): 구계정 DB listed 5,611 vs 채널 SALE 2,192 — 3,419건이 유령.

부풀린 숫자는 조용히 퍼진다.
  · 등록 한도 계산이 틀려 "한도 초과"로 신규 등록을 스스로 막는다
  · 로테이션·집계가 없는 상품을 살아있는 것으로 세고
  · 일괄 작업이 이미 없는 상품에 API 를 쏴 쿼터를 태운다

방식
----
채널 검색 API 로 SALE / SUSPENSION 전량을 받아 originProductNo 집합을 만들고,
DB 와 대조해 아래로 정정한다.
  채널 SALE        → listed
  채널 SUSPENSION  → paused
  어느 쪽도 아님    → removed (삭제됨)

★DB 만 고친다. 채널에 쓰기를 하지 않으므로 되돌리기는 백업 복원으로 충분하다.

사용:
    PYTHONPATH=<repo> .venv/bin/python -m backend.purchase.scripts.reconcile_smartstore_status \\
        --account old [--apply]
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
from backend.purchase.services.naver_commerce_service import (
    naver_account, _get_token, _request_with_retry, BASE,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("ss-reconcile")

PAGE_SIZE = 500          # 네이버 search 최대치. 페이지 수를 줄여 쿼터를 아낀다.
KST = timezone(timedelta(hours=9))


def fetch_product_nos(status: str) -> set[str]:
    """해당 상태의 originProductNo 전량. 실패하면 예외 — 부분 결과로 DB 를 고치면 안 된다."""
    token = _get_token()
    if not token:
        raise RuntimeError("네이버 토큰 발급 실패")
    out: set[str] = set()
    page = 1
    while True:
        r = _request_with_retry(
            "POST", BASE + "/v1/products/search",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json;charset=UTF-8"},
            json={"productStatusTypes": [status], "page": page, "size": PAGE_SIZE},
            timeout=30,
        )
        if r is None or r.status_code >= 400:
            raise RuntimeError(f"{status} {page}페이지 조회 실패 "
                               f"(HTTP {getattr(r, 'status_code', 'none')})")
        body = r.json() or {}
        for it in body.get("contents") or []:
            no = it.get("originProductNo")
            if no:
                out.add(str(no))
        total_pages = int(body.get("totalPages") or 1)
        logger.info("  %s %d/%d 페이지 · 누적 %d", status, page, total_pages, len(out))
        if page >= total_pages:
            break
        page += 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="old", choices=("old", "new"))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    with naver_account(args.account):
        sale = fetch_product_nos("SALE")
        susp = fetch_product_nos("SUSPENSION")
    logger.info("채널 실측: SALE %d · SUSPENSION %d", len(sale), len(susp))

    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT id, channel_product_id pno, status
                 FROM listings_pa
                WHERE channel='smartstore'
                  AND COALESCE(NULLIF(naver_account,''),'old')=?
                  AND status IN ('listed','paused')""", (args.account,)).fetchall()]

    plan = {"listed": [], "paused": [], "removed": []}
    for r in rows:
        pno = str(r["pno"] or "")
        want = "listed" if pno in sale else ("paused" if pno in susp else "removed")
        if want != r["status"]:
            plan[want].append(r["id"])

    logger.info("정정 대상 — listed %d · paused %d · removed %d",
                len(plan["listed"]), len(plan["paused"]), len(plan["removed"]))
    logger.info("변화 없음 %d건", len(rows) - sum(len(v) for v in plan.values()))

    if not args.apply:
        logger.info("DRY-RUN — 실제 적용하려면 --apply")
        return

    stamp = datetime.now(KST)
    backup = f"/home/ubuntu/charisg-deploy-backups/ss_status_before_{stamp:%Y%m%d_%H%M%S}.json"
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    with open(backup, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    logger.info("변경 전 상태 백업: %s", backup)

    reason = f"채널 실측 대조로 정정 ({stamp:%Y-%m-%d})"
    with get_db() as conn:
        for want, ids in plan.items():
            for i in range(0, len(ids), 500):     # 락 장기점유 방지
                chunk = ids[i:i + 500]
                conn.execute(
                    "UPDATE listings_pa SET status=?, error_message=?, "
                    "last_synced_at=datetime('now') WHERE id IN (%s)"
                    % ",".join("?" * len(chunk)),
                    (want, reason, *chunk),
                )
    logger.info("적용 완료")


if __name__ == "__main__":
    main()
