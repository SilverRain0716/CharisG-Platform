"""
tracking_service.py — 배송 추적 (CJ 자동 + 수동 입력).

CJ Open API webhook + 수동 트래킹 번호 등록 → orders 테이블 동기화.
구현 완료(모노리스 기준) — 로직 표면 인터페이스만 유지.
"""
import logging
from typing import Optional

from backend.purchase.database import get_db_hot as get_db

logger = logging.getLogger(__name__)


def update_order_tracking(
    order_id: int,
    forwarder_tracking: Optional[str] = None,
    domestic_tracking: Optional[str] = None,
    note: str = "",
) -> bool:
    with get_db() as conn:
        sets = []
        params = []
        if forwarder_tracking:
            sets.append("forwarder_tracking = ?")
            params.append(forwarder_tracking)
        if domestic_tracking:
            sets.append("domestic_tracking = ?")
            params.append(domestic_tracking)
        if not sets:
            return False
        params.append(order_id)
        conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id = ?", params)

        if note:
            conn.execute(
                """INSERT INTO order_steps (order_id, step, label, note)
                   VALUES (?, 'tracking_update', '추적 갱신', ?)""",
                (order_id, note),
            )
    return True


def get_order_with_tracking(order_id: int) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            """SELECT id, current_step, amazon_order_id, forwarder_tracking,
                      domestic_tracking, placed_at, completed_at
               FROM orders WHERE id=?""",
            (order_id,),
        ).fetchone()
        if not row:
            return None
        steps = conn.execute(
            "SELECT step, label, started_at, finished_at, note FROM order_steps WHERE order_id=? ORDER BY id",
            (order_id,),
        ).fetchall()
    return {**dict(row), "steps": [dict(s) for s in steps]}


def cj_webhook_handler(payload: dict) -> dict:
    """CJ Open API 추적 webhook 처리 (placeholder — EC2 deploy 후 시그니처 검증)."""
    cj_order_id = payload.get("orderId")
    tracking = payload.get("trackingNumber")
    status = payload.get("status")

    if not cj_order_id:
        return {"ok": False, "error": "orderId 누락"}

    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM orders WHERE amazon_order_id=?", (cj_order_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "주문 없음"}
        update_order_tracking(row["id"], forwarder_tracking=tracking, note=f"CJ status: {status}")
    return {"ok": True}
