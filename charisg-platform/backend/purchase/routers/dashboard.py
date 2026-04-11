"""PA Dashboard — 퍼널 + 할 일 + KPI + 알림."""
from fastapi import APIRouter, Depends

from backend.purchase.auth import current_user
from backend.purchase.database import get_db

router = APIRouter(prefix="/api/pa", tags=["pa-dashboard"])


@router.get("/dashboard")
def get_dashboard(user: dict = Depends(current_user)):
    with get_db() as conn:
        kw = conn.execute("SELECT COUNT(*) c FROM keywords").fetchone()["c"]
        sourcing = conn.execute("SELECT COUNT(*) c FROM sourcing_candidates").fetchone()["c"]
        margin_done = conn.execute("SELECT COUNT(*) c FROM margin_calcs").fetchone()["c"]
        customs_pass = conn.execute(
            "SELECT COUNT(*) c FROM customs_checks WHERE risk='PASS'"
        ).fetchone()["c"]
        go = conn.execute(
            "SELECT COUNT(*) c FROM sourcing_candidates WHERE sourcing_status='go'"
        ).fetchone()["c"]
        listed = conn.execute("SELECT COUNT(*) c FROM listings_pa WHERE status='listed'").fetchone()["c"]
        active = conn.execute("SELECT COUNT(*) c FROM products WHERE status='active'").fetchone()["c"]

        # 오늘의 할 일
        nogo_pending = conn.execute(
            "SELECT COUNT(*) c FROM sourcing_candidates WHERE sourcing_status='reviewed'"
        ).fetchone()["c"]
        upload_pending = conn.execute(
            "SELECT COUNT(*) c FROM upload_queue WHERE status='pending'"
        ).fetchone()["c"]
        cs_open = conn.execute(
            "SELECT COUNT(*) c FROM cs_tickets WHERE status='open'"
        ).fetchone()["c"]

        # KPI
        avg_margin = conn.execute(
            "SELECT AVG(seller_margin_pct) m FROM margin_calcs"
        ).fetchone()["m"] or 0

    return {
        "funnel": [
            {"key": "keywords",  "label": "키워드",   "count": kw},
            {"key": "sourcing",  "label": "소싱",    "count": sourcing},
            {"key": "margin",    "label": "마진",    "count": margin_done},
            {"key": "customs",   "label": "통관",    "count": customs_pass},
            {"key": "go",        "label": "GO",      "count": go},
            {"key": "listed",    "label": "등록",    "count": listed},
            {"key": "active",    "label": "활성",    "count": active},
        ],
        "todos": {
            "go_pending": nogo_pending,
            "upload_pending": upload_pending,
            "cs_open": cs_open,
        },
        "kpis": {
            "active_products": active,
            "avg_margin": round(avg_margin, 1),
        },
        "alerts": [],
    }
