"""
dedupe_naver_listings.py — 네이버 스마트스토어 중복 listing 정리.

중복 판정: 동일 products.asin이 listings_pa에 smartstore/listed 상태로 여러 건.
각 ASIN 그룹에서 **가장 먼저 등록된 것(products.id 최소) 하나만 유지**,
나머지는 SUSPENSION (판매 중지) + DB excluded 처리.

네이버 클린프로그램 중복상품 위반 해소용. 한 번 실행하면 전체 듀플리케이션
자동 정리.

사용:
    python3 -m backend.purchase.scripts.dedupe_naver_listings [--dry-run] [--limit N]
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

from backend.purchase.services.naver_commerce_service import (
    _get_token, _request_with_retry, _gate,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

NAVER_API = "https://api.commerce.naver.com/external/v1/products/origin-products"


def collect_duplicates(conn) -> list[dict]:
    """유지할 1건(keep) + 제거할 N건(remove) 그룹화."""
    rows = conn.execute(
        """SELECT p.asin, l.product_id, l.channel_product_id
           FROM listings_pa l JOIN products p ON p.id=l.product_id
           WHERE l.channel='smartstore' AND l.status='listed'
             AND p.asin IS NOT NULL AND p.asin != ''
           ORDER BY p.asin, p.id"""
    ).fetchall()

    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["asin"], []).append({
            "product_id": r["product_id"],
            "channel_product_id": r["channel_product_id"],
        })

    targets = []
    for asin, items in groups.items():
        if len(items) <= 1:
            continue
        keep = items[0]
        for dup in items[1:]:
            targets.append({"asin": asin, "keep": keep, "remove": dup})
    return targets


def suspend_one(token: str, channel_product_id: str) -> tuple[bool, str]:
    _gate()
    url = f"{NAVER_API}/{channel_product_id}/change-status"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = _request_with_retry("PUT", url, headers=headers, json={"statusType": "SUSPENSION"}, timeout=15)
    if r is None:
        return False, "no response"
    if r.status_code == 200:
        return True, ""
    return False, f"{r.status_code} {r.text[:120]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    db = Path(__file__).resolve().parents[1] / "purchase.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    targets = collect_duplicates(conn)
    if args.limit:
        targets = targets[: args.limit]
    logger.info(f"중복 제거 대상: {len(targets)}건 (각 ASIN 그룹에서 가장 오래된 1건 유지)")
    if args.dry_run:
        for t in targets[:10]:
            logger.info(f"  [DRY] asin={t['asin']} keep pid={t['keep']['product_id']} remove pid={t['remove']['product_id']} cpid={t['remove']['channel_product_id']}")
        return

    token = _get_token()
    if not token:
        logger.error("네이버 토큰 발급 실패")
        return

    ok = 0
    fail = 0
    fail_msgs = []
    for i, t in enumerate(targets):
        cpid = t["remove"]["channel_product_id"]
        if not cpid:
            # 이미 SUSPENSION된 경우 포함 가능 — skip
            continue
        success, err = suspend_one(token, cpid)
        if success:
            ok += 1
            # DB 업데이트
            conn.execute(
                """UPDATE listings_pa SET status='excluded',
                   error_message='네이버 중복 ASIN 정리 — 클린프로그램 대응',
                   last_synced_at=datetime('now')
                   WHERE channel='smartstore' AND product_id=?""",
                (t["remove"]["product_id"],),
            )
            conn.commit()
        else:
            fail += 1
            if len(fail_msgs) < 5:
                fail_msgs.append((cpid, err))

        if (i + 1) % 50 == 0:
            logger.info(f"  progress {i+1}/{len(targets)} ok={ok} fail={fail}")

    logger.info(f"완료: SUSPENSION {ok}, 실패 {fail}")
    for cpid, err in fail_msgs:
        logger.warning(f"  {cpid}: {err}")


if __name__ == "__main__":
    main()
