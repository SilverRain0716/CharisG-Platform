"""
order_receiver_service.py — 스마트스토어/쿠팡 주문 수신.

각 채널 webhook 또는 polling 으로 주문 수신 → orders 테이블 적재 + 6단계 워크플로우 시작.
"""
import json
import logging
from typing import Optional

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)

ORDER_STEPS = [
    ("order_received",   "주문 접수"),
    ("amazon_purchase",  "아마존 구매"),
    ("forwarder",        "배대지 입고"),
    ("international",    "국제 배송"),
    ("domestic",         "국내 배송"),
    ("completed",        "완료"),
]


def receive_order(
    channel: str,
    channel_order_id: str,
    product_id: Optional[int],
    customer_name: str,
    customer_phone: str,
    address: str,
    sale_price_krw: float,
    quantity: int = 1,
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO orders
               (channel, channel_order_id, product_id, customer_name, customer_phone,
                address, sale_price_krw, quantity, current_step)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'order_received')""",
            (channel, channel_order_id, product_id, customer_name, customer_phone,
             address, sale_price_krw, quantity),
        )
        if cur.lastrowid:
            order_id = cur.lastrowid
            conn.execute(
                "INSERT INTO order_steps (order_id, step, label) VALUES (?, 'order_received', '주문 접수')",
                (order_id,),
            )
            return order_id
        existing = conn.execute(
            "SELECT id FROM orders WHERE channel=? AND channel_order_id=?",
            (channel, channel_order_id),
        ).fetchone()
        return existing["id"] if existing else 0


def advance_step(order_id: int, new_step: str, note: str = "") -> bool:
    valid = {s for s, _ in ORDER_STEPS}
    if new_step not in valid:
        return False

    label = next((l for s, l in ORDER_STEPS if s == new_step), new_step)
    with get_db() as conn:
        # 이전 step 종료
        conn.execute(
            """UPDATE order_steps SET finished_at=CURRENT_TIMESTAMP
               WHERE order_id=? AND finished_at IS NULL""",
            (order_id,),
        )
        # 새 step 시작
        conn.execute(
            "INSERT INTO order_steps (order_id, step, label, note) VALUES (?, ?, ?, ?)",
            (order_id, new_step, label, note),
        )
        # 주문 current_step 갱신
        conn.execute(
            "UPDATE orders SET current_step=? WHERE id=?",
            (new_step, order_id),
        )
        if new_step == "completed":
            conn.execute(
                "UPDATE orders SET completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (order_id,),
            )
    return True
