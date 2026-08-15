"""쿠팡 listings_pa pending 7일+ 자동 분류 — 영구 오류 excluded 이관.

분류 규칙 (현재 단순 — error_message NULL 패턴 다수 발견):
  - status='pending' AND channel_product_id IS NULL AND created_at 7일 초과
    → status='excluded' (lister API 호출 자체 0회 = root_cause_unknown)

매일 KST 00:30 (quota_retry 25분 후) systemd timer 로 실행.
quota_retry 가 한도 외 영구 오류분도 retry 하지 않도록 사전 정리.

실행: python -m backend.purchase.scripts.classify_stale_pending
"""
import logging

from backend.purchase.database import get_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("classify-stale-pending")


def main() -> None:
    with get_db() as conn:
        # 대상 카운트
        target = conn.execute(
            """SELECT COUNT(*) FROM listings_pa
               WHERE channel='coupang' AND status='pending'
                 AND channel_product_id IS NULL
                 AND julianday('now') - julianday(created_at) > 7"""
        ).fetchone()[0]
        logger.info(f"대상 (7일+ no_listing_attempt): {target}건")

        if target == 0:
            logger.info("이관 0건 — 종료")
            return

        cur = conn.execute(
            """UPDATE listings_pa
               SET status='excluded',
                   error_message='auto-classified: 7d+ no_listing_attempt',
                   last_synced_at=CURRENT_TIMESTAMP
               WHERE channel='coupang' AND status='pending'
                 AND channel_product_id IS NULL
                 AND julianday('now') - julianday(created_at) > 7"""
        )
        logger.info(f"excluded 이관: {cur.rowcount}건")

        # 잔여 pending 분포
        rows = conn.execute(
            """SELECT date(created_at) AS d, COUNT(*) AS cnt
               FROM listings_pa WHERE channel='coupang' AND status='pending'
               GROUP BY d ORDER BY d DESC"""
        ).fetchall()
        for r in rows:
            logger.info(f"  잔여 pending {r['d']}: {r['cnt']}건")


if __name__ == "__main__":
    main()
