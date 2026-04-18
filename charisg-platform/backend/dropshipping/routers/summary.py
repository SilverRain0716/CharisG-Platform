"""DS summary — Hub 카드용 요약 (인증 없이 호출 가능, 같은 머신에서만)."""
from fastapi import APIRouter

from backend.dropshipping.database import get_db

router = APIRouter(prefix="/api/ds", tags=["ds-summary"])


@router.get("/summary")
def ds_summary():
    with get_db() as conn:
        active = conn.execute(
            "SELECT COUNT(*) c FROM collected_products WHERE status='active'"
        ).fetchone()["c"]
        # ⚠️ go_decision 은 monolith 마이그레이션 스테일 데이터 가능성 → hard_filter_pass=1 가드 필수
        go = conn.execute(
            "SELECT COUNT(*) c FROM collected_products "
            "WHERE go_decision IN ('GO','GO_ORGANIC') AND hard_filter_pass=1"
        ).fetchone()["c"]
        avg_margin_row = conn.execute(
            "SELECT AVG(real_margin_pct) m FROM collected_products "
            "WHERE go_decision='GO' AND hard_filter_pass=1"
        ).fetchone()
        avg_margin = avg_margin_row["m"] if avg_margin_row and avg_margin_row["m"] is not None else 0

        revenue_row = conn.execute(
            "SELECT COALESCE(SUM(total_revenue), 0) r FROM sales"
        ).fetchone()
        total_revenue = revenue_row["r"] if revenue_row else 0

        listed = conn.execute(
            "SELECT COUNT(*) c FROM listings WHERE status='listed'"
        ).fetchone()["c"]

        health = conn.execute(
            "SELECT odr, late_shipment_rate, cancel_rate, valid_tracking_rate "
            "FROM account_health ORDER BY id DESC LIMIT 1"
        ).fetchone()

    return {
        "active_products": active,
        "go_count": go,
        "avg_margin": round(avg_margin, 1),
        "total_revenue": total_revenue,
        "pendingCount": listed,
        "account_health": "OK" if (health and (health["odr"] or 0) < 1.0) else None,
        "kpis": [
            {"label": "활성 상품", "value": active},
            {"label": "GO",       "value": go},
            {"label": "평균 마진", "value": f"{round(avg_margin, 1)}%"},
        ],
    }
