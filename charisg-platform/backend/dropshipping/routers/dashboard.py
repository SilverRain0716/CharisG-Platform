"""DS Dashboard — 퍼널 + KPI + 알림 (View 1: Pipeline Overview)."""
from fastapi import APIRouter, Depends

from backend.dropshipping.auth import current_user
from backend.dropshipping.database import get_db

router = APIRouter(prefix="/api/ds", tags=["ds-dashboard"])


@router.get("/dashboard")
def get_dashboard(user: dict = Depends(current_user)):
    with get_db() as conn:
        # 파이프라인 단계별 카운트
        total_collected = conn.execute(
            "SELECT COUNT(*) c FROM collected_products"
        ).fetchone()["c"]

        filter_passed = conn.execute(
            "SELECT COUNT(*) c FROM collected_products WHERE hard_filter_pass=1"
        ).fetchone()["c"]

        go_count = conn.execute(
            "SELECT COUNT(*) c FROM collected_products WHERE go_decision='GO'"
        ).fetchone()["c"]

        listed = conn.execute(
            "SELECT COUNT(*) c FROM listings WHERE status IN ('listed','active')"
        ).fetchone()["c"]

        active = conn.execute(
            "SELECT COUNT(*) c FROM listings WHERE status='active'"
        ).fetchone()["c"]

        # KPI
        avg_margin = conn.execute(
            "SELECT AVG(real_margin_pct) m FROM collected_products WHERE go_decision='GO'"
        ).fetchone()["m"] or 0

        # 알림
        alerts_raw = []
        low_stock = conn.execute(
            "SELECT product_name FROM collected_products "
            "WHERE business_model='dropship' AND status='active' AND stock_quantity < 10 "
            "LIMIT 5"
        ).fetchall()
        for r in low_stock:
            alerts_raw.append({"type": "warn", "title": "재고 부족", "message": r["product_name"]})

    return {
        "funnel": [
            {"key": "cj_total",    "label": "CJ 38K",    "count": 38000},
            {"key": "collected",   "label": "Collected", "count": total_collected},
            {"key": "filter",      "label": "Filter",    "count": filter_passed},
            {"key": "go",          "label": "GO",        "count": go_count},
            {"key": "listed",      "label": "Listed",    "count": listed},
            {"key": "active",      "label": "Active",    "count": active},
        ],
        "kpis": {
            "go_count": go_count,
            "avg_margin": round(avg_margin, 1),
            "listing_progress": f"{listed}/{go_count}" if go_count else "0/0",
            "active_products": active,
        },
        "alerts": [{"id": i, **a, "at": ""} for i, a in enumerate(alerts_raw)],
    }
