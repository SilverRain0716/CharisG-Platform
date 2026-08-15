"""PA summary — Hub 카드용."""
from fastapi import APIRouter

from backend.purchase.database import get_db, get_db_hot

router = APIRouter(prefix="/api/pa", tags=["pa-summary"])


@router.get("/summary")
def pa_summary():
    # cold.db: products, listings_pa
    with get_db() as conn:
        active = conn.execute("SELECT COUNT(*) c FROM products WHERE status IN ('listed','active')").fetchone()["c"]
        # 평균 마진 — sale_krw 가중평균 (매출 1원당 마진율). 단순 산술 평균은
        # 가격대 무관 평균이라 매출 KPI 와 의미 어긋남.
        avg_margin_row = conn.execute(
            "SELECT 100.0 * SUM(net_margin_krw) / NULLIF(SUM(sale_krw), 0) AS m "
            "FROM listings_pa "
            "WHERE status IN ('listed','active') AND net_margin_pct IS NOT NULL "
            "AND sale_krw > 0"
        ).fetchone()
        avg_margin = avg_margin_row["m"] if avg_margin_row and avg_margin_row["m"] is not None else 0

    # hot.db: orders, cs_tickets
    with get_db_hot() as conn:
        # 대기 주문: completed/cancelled 모두 제외
        pending_orders = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE current_step NOT IN ('completed','cancelled')"
        ).fetchone()["c"]
        pending_cs = conn.execute(
            "SELECT COUNT(*) c FROM cs_tickets WHERE status IN ('open','in_progress')"
        ).fetchone()["c"]
        # 이번 달 매출 (KST 결제일 기준 GMV — cancelled 제외)
        revenue_row = conn.execute(
            "SELECT COALESCE(SUM(sale_price_krw), 0) r FROM orders "
            "WHERE current_step != 'cancelled' "
            "AND date(placed_at, '+9 hours') >= date('now', '+9 hours', 'start of month')"
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
