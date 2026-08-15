"""enqueue_regroup_candidates.py — 기존 단편화 단일상품 전체 재그룹 enqueue.

같은 아마존 parent_asin 을 공유하는데 쿠팡에 따로 등록된 단일 listed 상품들을
group_registration_queue 에 enqueue → 드레인 워커가 청소 큐 비운 뒤(id 순서) 이어서
그룹으로 재등록한다. 워커가 children 을 parent_asin 으로 찾으므로 큐엔 parent_asin 을 넣는다.

후보 = parent_asin 별로 (이미 그룹인 것 제외한) 쿠팡 listed 단일이 2개 이상,
       AND 그 parent_asin 이 아직 group_registration_queue 에 없는 것(중복방지).

사용: python -m backend.purchase.scripts.enqueue_regroup_candidates [--apply] [--sheet regroup:phase1]
기본 dry-run(카운트만). --apply 로 실제 INSERT.
"""
from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))

import logging

from backend.purchase.database import get_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("enqueue-regroup")

_CANDIDATE_SQL = """
WITH grouped AS (
  SELECT listing_id FROM listing_options GROUP BY listing_id HAVING COUNT(*) >= 2
),
cp_single AS (
  SELECT p.parent_asin AS parent, p.asin AS child
  FROM listings_pa l JOIN products p ON p.id = l.product_id
  WHERE l.channel = 'coupang' AND l.status = 'listed'
    AND p.parent_asin IS NOT NULL AND p.parent_asin != ''
    AND l.id NOT IN (SELECT listing_id FROM grouped)
),
multi AS (
  SELECT parent FROM cp_single GROUP BY parent HAVING COUNT(DISTINCT child) >= 2
)
SELECT parent FROM multi
WHERE parent NOT IN (
  SELECT parent_asin FROM group_registration_queue WHERE parent_asin IS NOT NULL
)
ORDER BY parent
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제 INSERT (없으면 dry-run)")
    ap.add_argument("--sheet", default="regroup:phase1")
    args = ap.parse_args()

    with get_db() as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        parents = [r["parent"] for r in conn.execute(_CANDIDATE_SQL).fetchall()]

    logger.info("재그룹 후보 parent_asin: %d개 (이미 큐에 있는 것 제외, sheet=%s, apply=%s)",
                len(parents), args.sheet, args.apply)
    if not parents:
        logger.info("후보 없음 — 종료")
        return
    logger.info("샘플 5: %s", parents[:5])

    if not args.apply:
        logger.info("=== DRY-RUN — INSERT 안 함. --apply 로 실제 enqueue ===")
        return

    inserted = 0
    with get_db() as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        for pa in parents:
            # 워커 enqueue 와 동일한 중복 가드
            exists = conn.execute(
                "SELECT 1 FROM group_registration_queue WHERE parent_asin=? "
                "AND status IN ('queued','pre_scanning','registering')",
                (pa,),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO group_registration_queue "
                "(parent_asin, sheet_id, status, requested, channels, queued_at) "
                "VALUES (?, ?, 'queued', 1, 'coupang', datetime('now'))",
                (pa, args.sheet),
            )
            inserted += 1
    logger.info("=== enqueue 완료: %d개 parent_asin → %s (status=queued) ===", inserted, args.sheet)


if __name__ == "__main__":
    main()
