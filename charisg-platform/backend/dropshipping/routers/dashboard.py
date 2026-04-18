"""DS Dashboard — 퍼널 + KPI + 알림 (View 1: Pipeline Overview).

⚠️ 데이터 위험 주의 (2026-04-16):
  collected_products.go_decision 필드는 monolith 마이그레이션에서 들어온 값으로,
  Hard Filter(blocked_cat / branded / blocked_keyword) 위반 상품이 과거에는 GO 로
  기록돼 있었음. 2026-04-16 에 go_decision='GO' AND hard_filter_pass=0 인 166개를
  'BLOCKED' 로 재분류함. 신규 마이그레이션으로 다시 오염될 수 있으므로 GO 카운트
  쿼리는 반드시 hard_filter_pass=1 조건을 함께 걸어야 한다.
"""
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
            "SELECT COUNT(*) c FROM collected_products "
            "WHERE go_decision='GO' AND hard_filter_pass=1"
        ).fetchone()["c"]

        listed = conn.execute(
            "SELECT COUNT(*) c FROM listings WHERE status IN ('listed','active')"
        ).fetchone()["c"]

        active = conn.execute(
            "SELECT COUNT(*) c FROM listings WHERE status='active'"
        ).fetchone()["c"]

        asin_matched = conn.execute(
            "SELECT COUNT(*) c FROM collected_products "
            "WHERE hard_filter_pass=1 AND matched_asin IS NOT NULL AND matched_asin != ''"
        ).fetchone()["c"]

        # KPI
        avg_margin = conn.execute(
            "SELECT AVG(real_margin_pct) m FROM collected_products "
            "WHERE go_decision='GO' AND hard_filter_pass=1"
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
            {"key": "cj_total",    "label": "CJ 수집",     "count": total_collected},
            {"key": "filter",      "label": "필터 통과",   "count": filter_passed},
            {"key": "go",          "label": "GO 판정",     "count": go_count},
            {"key": "matched",     "label": "ASIN 매칭",   "count": asin_matched},
            {"key": "listed",      "label": "Amazon 등록", "count": listed},
            {"key": "active",      "label": "활성",        "count": active},
        ],
        "kpis": {
            "go_count": go_count,
            "avg_margin": round(avg_margin, 1),
            "listing_progress": f"{listed}/{go_count}" if go_count else "0/0",
            "active_products": active,
        },
        "alerts": [{"id": i, **a, "at": ""} for i, a in enumerate(alerts_raw)],
    }
