"""PA Dashboard — 퍼널 + 할 일 + KPI + 알림."""
from fastapi import APIRouter, Depends

from backend.purchase.auth import current_user
from backend.purchase.database import get_db, get_db_hot

router = APIRouter(prefix="/api/pa", tags=["pa-dashboard"])


@router.get("/dashboard")
def get_dashboard(user: dict = Depends(current_user)):
    # cold.db (대량 처리 데이터)
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
        active = conn.execute("SELECT COUNT(*) c FROM products WHERE status IN ('listed','active')").fetchone()["c"]

        nogo_pending = conn.execute(
            "SELECT COUNT(*) c FROM sourcing_candidates WHERE sourcing_status='reviewed'"
        ).fetchone()["c"]
        upload_pending = conn.execute(
            "SELECT COUNT(*) c FROM upload_queue WHERE status='pending'"
        ).fetchone()["c"]

        avg_margin = conn.execute(
            "SELECT AVG(seller_margin_pct) m FROM margin_calcs"
        ).fetchone()["m"] or 0

        # 마지막 쿠팡 주문 동기화 (batch_jobs 는 cold)
        last_sync_row = conn.execute(
            """SELECT status, phase_message, finished_at FROM batch_jobs
               WHERE job_type='coupang_order_sync'
               ORDER BY COALESCE(finished_at, started_at, created_at) DESC LIMIT 1"""
        ).fetchone()
        last_ss_sync_row = conn.execute(
            """SELECT status, phase_message, finished_at FROM batch_jobs
               WHERE job_type='smartstore_order_sync'
               ORDER BY COALESCE(finished_at, started_at, created_at) DESC LIMIT 1"""
        ).fetchone()

    # hot.db (실시간 운영 — orders, cs_tickets)
    with get_db_hot() as conn:
        cs_open = conn.execute(
            "SELECT COUNT(*) c FROM cs_tickets WHERE status='open'"
        ).fetchone()["c"]
        orders_today = conn.execute(
            "SELECT COUNT(*) c FROM orders "
            "WHERE date(placed_at, '+9 hours') = date('now', '+9 hours')"
        ).fetchone()["c"]
        orders_pending = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE current_step='order_received'"
        ).fetchone()["c"]
        by_channel_rows = conn.execute(
            "SELECT channel, COUNT(*) c FROM orders GROUP BY channel"
        ).fetchall()
        orders_by_channel = {r["channel"] or "unknown": r["c"] for r in by_channel_rows}
    last_coupang_sync = dict(last_sync_row) if last_sync_row else None
    last_smartstore_sync = dict(last_ss_sync_row) if last_ss_sync_row else None

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
            "orders_pending": orders_pending,
        },
        "kpis": {
            "active_products": active,
            "avg_margin": round(avg_margin, 1),
            "orders_today": orders_today,
            "orders_by_channel": orders_by_channel,
        },
        "last_coupang_sync": last_coupang_sync,
        "last_smartstore_sync": last_smartstore_sync,
        "alerts": [],
    }
