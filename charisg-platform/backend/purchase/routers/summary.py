"""PA summary — Hub 카드용."""
from fastapi import APIRouter

from backend.purchase.database import get_db, get_db_hot

router = APIRouter(prefix="/api/pa", tags=["pa-summary"])


@router.get("/summary")
def pa_summary():
    # cold.db: products, margin_calcs
    with get_db() as conn:
        active = conn.execute("SELECT COUNT(*) c FROM products WHERE status IN ('listed','active')").fetchone()["c"]
        avg_margin_row = conn.execute(
            "SELECT AVG(seller_margin_pct) m FROM margin_calcs"
        ).fetchone()
        avg_margin = avg_margin_row["m"] if avg_margin_row and avg_margin_row["m"] is not None else 0

    # hot.db: orders, cs_tickets
    with get_db_hot() as conn:
        pending_orders = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE current_step NOT IN ('completed')"
        ).fetchone()["c"]
        pending_cs = conn.execute(
            "SELECT COUNT(*) c FROM cs_tickets WHERE status IN ('open','in_progress')"
        ).fetchone()["c"]
        revenue_row = conn.execute(
            "SELECT COALESCE(SUM(sale_price_krw), 0) r FROM orders WHERE current_step='completed' "
            "AND date(completed_at) >= date('now', 'start of month')"
        ).fetchone()
        monthly_revenue = revenue_row["r"] if revenue_row else 0

    return {
        "active_products": active,
        "monthly_revenue": monthly_revenue,
        "avg_margin": round(avg_margin, 1),
        "pending_orders": pending_orders,
        "pending_cs": pending_cs,
        "pendingCount": pending_orders + pending_cs,
        "kpis": [
            {"label": "활성 상품", "value": active},
            {"label": "대기 주문", "value": pending_orders},
            {"label": "미처리 CS", "value": pending_cs},
        ],
    }
