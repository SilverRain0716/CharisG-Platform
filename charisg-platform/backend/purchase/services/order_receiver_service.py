"""
order_receiver_service.py — 스마트스토어/쿠팡 주문 수신.

각 채널 webhook 또는 polling 으로 주문 수신 → orders 테이블 적재 + 6단계 워크플로우 시작.
"""
import json
import logging
from typing import Optional

from backend.purchase.database import get_db, get_db_hot

logger = logging.getLogger(__name__)


def _lookup_product_denorm(product_id: Optional[int]) -> dict:
    """cold.db 에서 product 정보 lookup → orders 의 denormalized 컬럼용.

    product_id None 이면 빈 dict.
    """
    if not product_id:
        return {}
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT title_ko, title_en, brand, images_json, asin FROM products WHERE id=?",
                (product_id,),
            ).fetchone()
        if not row:
            return {}
        title = row["title_ko"] or row["title_en"] or ""
        first_img = ""
        try:
            imgs = json.loads(row["images_json"] or "[]")
            if imgs:
                first_img = imgs[0] if isinstance(imgs[0], str) else (imgs[0].get("url") or "")
        except Exception:
            pass
        return {
            "product_name_cache": title[:200] if title else None,
            "product_image_cache": first_img[:500] if first_img else None,
            "brand_cache": row["brand"],
            "asin_cache": row["asin"],
        }
    except Exception as e:
        logger.warning(f"[order-receiver] product {product_id} denorm lookup 실패: {e}")
        return {}

ORDER_STEPS = [
    ("order_received",   "주문 접수"),
    ("amazon_purchase",  "아마존 구매"),
    ("forwarder",        "배대지 입고"),
    ("international",    "국제 배송"),
    ("domestic",         "국내 배송"),
    ("completed",        "완료"),
    ("cancelled",        "취소"),
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
    customs_clearance_code: Optional[str] = None,
    orderer_real_phone: Optional[str] = None,
    shipping_message: Optional[str] = None,
    external_sku: Optional[str] = None,
    ordered_at: Optional[str] = None,
    paid_at: Optional[str] = None,
    child_product_id: Optional[int] = None,
    child_asin: Optional[str] = None,
) -> tuple[int, bool]:
    """신규 INSERT 시 (order_id, True), 이미 존재 시 (order_id, False).

    v13 컬럼(customs_clearance_code 등)은 신규 삽입에만 적용 — 기존 주문은 유지.
    v18: child_product_id / child_asin (multi-option 등록 시 어느 옵션 팔렸는지).
    """
    # Denormalized 정보 미리 lookup (cold.db) — hot.db INSERT 시 cache 컬럼 채움
    denorm = _lookup_product_denorm(product_id)

    with get_db_hot() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO orders
               (channel, channel_order_id, product_id, customer_name, customer_phone,
                address, sale_price_krw, quantity, current_step,
                customs_clearance_code, orderer_real_phone, shipping_message,
                external_sku, ordered_at, paid_at,
                child_product_id, child_asin,
                product_name_cache, product_image_cache, brand_cache, asin_cache)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'order_received', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (channel, channel_order_id, product_id, customer_name, customer_phone,
             address, sale_price_krw, quantity,
             customs_clearance_code, orderer_real_phone, shipping_message,
             external_sku, ordered_at, paid_at,
             child_product_id, child_asin,
             denorm.get("product_name_cache"), denorm.get("product_image_cache"),
             denorm.get("brand_cache"), denorm.get("asin_cache")),
        )
        if cur.lastrowid:
            order_id = cur.lastrowid
            conn.execute(
                "INSERT INTO order_steps (order_id, step, label) VALUES (?, 'order_received', '주문 접수')",
                (order_id,),
            )
            return order_id, True
        existing = conn.execute(
            "SELECT id FROM orders WHERE channel=? AND channel_order_id=?",
            (channel, channel_order_id),
        ).fetchone()
        return (existing["id"] if existing else 0), False


def advance_step(order_id: int, new_step: str, note: str = "") -> bool:
    valid = {s for s, _ in ORDER_STEPS}
    if new_step not in valid:
        return False

    label = next((l for s, l in ORDER_STEPS if s == new_step), new_step)
    with get_db_hot() as conn:
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
