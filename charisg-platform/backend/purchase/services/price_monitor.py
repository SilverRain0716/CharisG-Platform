"""
price_monitor.py — 아마존 가격 변동 → 마진 재계산 → 재가격 제안.
"""
import logging
from typing import Optional

from backend.purchase.database import get_db
from backend.purchase.services.margin_calculator import calculate_with_defaults

logger = logging.getLogger(__name__)

# 마진 임계치 — 이하로 떨어지면 alert
MARGIN_FLOOR_PCT = 15.0


def record_price(product_id: int, amazon_price_usd: float, fx_rate: float) -> None:
    """가격 이력 저장 + 현재 마진 재계산."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT sale_price_krw FROM products WHERE id=?", (product_id,),
        ).fetchone()
        if not row:
            return
        sale_krw = row["sale_price_krw"] or 0

    if sale_krw <= 0:
        return

    result = calculate_with_defaults(amazon_price_usd, sale_krw)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO price_history (product_id, amazon_price_usd, fx_rate, margin_pct)
               VALUES (?, ?, ?, ?)""",
            (product_id, amazon_price_usd, fx_rate, result.seller_margin_pct),
        )

    if result.seller_margin_pct < MARGIN_FLOOR_PCT:
        logger.warning(
            f"product {product_id}: margin {result.seller_margin_pct:.1f}% < floor {MARGIN_FLOOR_PCT}%"
        )


def get_margin_alerts() -> list[dict]:
    """최근 가격 이력 기준 마진 < floor 인 상품."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT ph.product_id, p.title_ko, ph.margin_pct, ph.captured_at
               FROM price_history ph
               JOIN products p ON ph.product_id = p.id
               WHERE ph.margin_pct < ?
               ORDER BY ph.captured_at DESC LIMIT 50""",
            (MARGIN_FLOOR_PCT,),
        ).fetchall()
    return [dict(r) for r in rows]
