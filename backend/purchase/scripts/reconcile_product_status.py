"""products.status drift 자동 reconcile (일일 cron).

규칙:
  - listings_pa 에 listed/active 채널이 1개 이상이면 products.status = 'listed'
  - 위 조건 false 면:
      * 채널 중 paused 가 1개라도 있으면 'paused'
      * 그 외 → 'archived'  (rotated/archived/excluded 모두 archived 로 통합)
  - 채널 listing 자체가 없는 product 는 변경 안 함 (draft/ready 상태 보존)
  - products.status='draft'/'ready'/'removed' 는 무시 (수동 상태)

기존 sync 결함:
  - coupang_lister._sync_product_status / smartstore_lister 가 'listed' 방향만 sync
  - 채널이 paused/archived 로 빠질 때 products.status 가 drift
  - 2026-05-21 1회 cleanup 9,970행 후 본 cron 으로 drift 재발 방지
"""
import logging
import sys
import sqlite3
from pathlib import Path

# venv 보장
sys.path.insert(0, "/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, "/home/ubuntu/CharisG-Platform/charisg-platform/packages/backend-shared")

from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")

DB_PATH = Path("/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("reconcile-product-status")


def main():
    if not DB_PATH.exists():
        logger.error(f"DB 경로 없음: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.row_factory = sqlite3.Row

    # 1) listed → 비listed drift (오늘 cleanup 같은 패턴)
    listed_drift_sql = """
        UPDATE products
           SET status = CASE
             WHEN EXISTS (SELECT 1 FROM listings_pa l
                           WHERE l.product_id=products.id AND l.status='paused') THEN 'paused'
             ELSE 'archived'
           END,
           updated_at = CURRENT_TIMESTAMP
         WHERE status='listed'
           AND NOT EXISTS (SELECT 1 FROM listings_pa l2
                            WHERE l2.product_id=products.id
                              AND l2.status IN ('listed','active'))
    """
    cur = conn.execute(listed_drift_sql)
    listed_drift = cur.rowcount

    # 2) paused → listed 회복 (수동/sync 누락된 unpause 사례)
    paused_recover_sql = """
        UPDATE products
           SET status='listed', updated_at=CURRENT_TIMESTAMP
         WHERE status IN ('paused','archived')
           AND EXISTS (SELECT 1 FROM listings_pa l
                        WHERE l.product_id=products.id
                          AND l.status IN ('listed','active'))
    """
    cur = conn.execute(paused_recover_sql)
    recovered = cur.rowcount

    # 3) paused → archived (모든 채널이 paused 가 아니게 됐을 때)
    paused_to_archived_sql = """
        UPDATE products
           SET status='archived', updated_at=CURRENT_TIMESTAMP
         WHERE status='paused'
           AND NOT EXISTS (SELECT 1 FROM listings_pa l
                            WHERE l.product_id=products.id AND l.status='paused')
           AND NOT EXISTS (SELECT 1 FROM listings_pa l2
                            WHERE l2.product_id=products.id
                              AND l2.status IN ('listed','active'))
    """
    cur = conn.execute(paused_to_archived_sql)
    paused_to_arch = cur.rowcount

    # 4) orphan batch_jobs cleanup
    # 2026-05-21: pa-api 재시작/크래시 시 batch_jobs 가 status='running' 으로 남아 워커가
    # 다시 픽업하지 못하는 케이스 누적 (오늘 11건 발견). reconcile cron 에 watchdog 통합.
    # 기준: started_at 보다 4시간 이상 경과 + finished_at NULL → orphan 으로 판정.
    # 정상 ai_detail 도 4h 안엔 끝남. 보수적 임계값.
    orphan_long_sql = """
        UPDATE batch_jobs
           SET status='error',
               finished_at=datetime('now'),
               error_message=COALESCE(error_message, '')
                              || '[reconcile-cron] orphan running 잡 (>4h, finished_at NULL) 자동 정리'
         WHERE status='running'
           AND started_at IS NOT NULL
           AND finished_at IS NULL
           AND datetime(started_at) < datetime('now','-4 hours')
    """
    cur = conn.execute(orphan_long_sql)
    orphan_long = cur.rowcount

    # 5) 비일관 상태 (finished_at 채워졌는데 status='running')
    orphan_finished_sql = """
        UPDATE batch_jobs
           SET status='error',
               error_message=COALESCE(error_message, '')
                              || '[reconcile-cron] finished_at 있는데 status=running 비일관 정리'
         WHERE status='running' AND finished_at IS NOT NULL
    """
    cur = conn.execute(orphan_finished_sql)
    orphan_finished = cur.rowcount

    conn.commit()

    # snapshot
    rows = conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM products GROUP BY status ORDER BY cnt DESC"
    ).fetchall()
    snap = ", ".join(f"{r['status']}={r['cnt']}" for r in rows)

    logger.info(
        f"reconcile 완료 — listed→비listed: {listed_drift}건, "
        f"비listed→listed 회복: {recovered}건, "
        f"paused→archived: {paused_to_arch}건, "
        f"orphan jobs(>4h): {orphan_long}건, "
        f"비일관 jobs: {orphan_finished}건"
    )
    logger.info(f"snapshot: {snap}")
    conn.close()


if __name__ == "__main__":
    main()
