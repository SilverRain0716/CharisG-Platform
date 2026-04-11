"""DS CJ — Hard Filter 8 + 카탈로그 동기화 트리거."""
from fastapi import APIRouter, BackgroundTasks, Depends

from backend.dropshipping.auth import current_user
from backend.dropshipping.database import get_db

router = APIRouter(prefix="/api/ds/cj", tags=["ds-cj"])


@router.get("/hard-filter")
def get_hard_filter_config(user: dict = Depends(current_user)):
    """Hard Filter 8개 조건 (스펙 v1.0)."""
    return {
        "filters": [
            {"id": "us_warehouse",   "label": "US 창고",       "rule": "us_warehouse = True"},
            {"id": "real_margin",    "label": "실질 마진",     "rule": "real_margin_pct >= 25"},
            {"id": "stock",          "label": "재고",          "rule": "stock_quantity >= 10"},
            {"id": "price_range",    "label": "가격대",        "rule": "$15 <= calculated_price <= $70"},
            {"id": "weight",         "label": "무게",          "rule": "weight_g <= 2000"},
            {"id": "image_count",    "label": "이미지",        "rule": "image_count >= 3"},
            {"id": "blocked_brand",  "label": "브랜드 제외",   "rule": "BLOCKED_BRANDS not in title"},
            {"id": "blocked_category", "label": "카테고리 제외", "rule": "category not in {Health, Clothing}"},
        ],
    }


@router.get("/stats")
def get_cj_stats(user: dict = Depends(current_user)):
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM collected_products WHERE source='cj'").fetchone()["c"]
        passed = conn.execute("SELECT COUNT(*) c FROM collected_products WHERE source='cj' AND hard_filter_pass=1").fetchone()["c"]
        in_stock_low = conn.execute(
            "SELECT COUNT(*) c FROM collected_products WHERE source='cj' AND stock_quantity < 10"
        ).fetchone()["c"]
    return {
        "total_collected": total,
        "filter_passed": passed,
        "low_stock_count": in_stock_low,
    }


@router.post("/sync")
def trigger_cj_sync(background: BackgroundTasks, user: dict = Depends(current_user)):
    """CJ 카탈로그 동기화 트리거 (placeholder — EC2에서 실제 cj_service 호출)."""
    return {"started": True, "message": "CJ 동기화는 GitHub Actions 또는 EC2 cron 으로 실행됩니다"}
