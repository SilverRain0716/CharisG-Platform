"""
stock_monitor_service.py — 재고 모니터링 (네이버/쿠팡 리스팅 비활성화 자동 처리).

주기적 (cron) 으로 listings_pa 의 채널별 상태를 확인 → 품절 시 stock_alerts + 비활성화.
EC2 deploy 후 cron 으로 활성화.
"""
import logging
from typing import Optional

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)


def detect_out_of_stock() -> list[int]:
    """현재 active 상태인 listings_pa 중 품절 의심 상품 ID 리스트.

    실제 채널 API 호출은 EC2 deploy 후 활성화.
    여기서는 sourcing_candidates.in_stock 0 인 상품 기준.
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT p.id FROM products p
               LEFT JOIN sourcing_candidates s ON p.sourcing_id = s.id
               WHERE p.status='active' AND s.in_stock=0"""
        ).fetchall()
    return [r["id"] for r in rows]


def mark_out_of_stock(product_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO stock_alerts (product_id, type) VALUES (?, 'out_of_stock')",
            (product_id,),
        )
        conn.execute(
            "UPDATE products SET status='paused', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (product_id,),
        )
        conn.execute(
            """UPDATE listings_pa SET status='paused'
               WHERE product_id=? AND status IN ('listed','active')""",
            (product_id,),
        )
        logger.info(f"상품 {product_id} 품절 처리")


def run_monitor() -> dict:
    ids = detect_out_of_stock()
    for pid in ids:
        mark_out_of_stock(pid)
    return {"checked_at": "now", "out_of_stock_count": len(ids)}
