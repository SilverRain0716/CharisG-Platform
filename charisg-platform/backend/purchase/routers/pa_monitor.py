"""PA Monitor — 가격 변동 + 재고 + 경쟁가."""
from fastapi import APIRouter, Depends

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services.image_downloader import cleanup_expired_images
from backend.purchase.services.stock_monitor_service import run_monitor
from backend.purchase.services.price_monitor import get_margin_alerts

router = APIRouter(prefix="/api/pa/monitor", tags=["pa-monitor"])


@router.get("/stock")
def stock_alerts(user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT sa.*, p.title_ko FROM stock_alerts sa
               JOIN products p ON sa.product_id=p.id
               ORDER BY detected_at DESC LIMIT 100"""
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/stock/run")
def run_stock_monitor(user: dict = Depends(current_user)):
    return run_monitor()


@router.get("/margin")
def margin_alerts(user: dict = Depends(current_user)):
    return get_margin_alerts()


@router.post("/image-cleanup")
def image_cleanup(user: dict = Depends(current_user)):
    """만료된 이미지 정리 (cron 또는 수동 호출)."""
    return cleanup_expired_images()


@router.get("/price-history/{product_id}")
def price_history(product_id: int, user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT amazon_price_usd, fx_rate, margin_pct, captured_at
               FROM price_history WHERE product_id=? ORDER BY captured_at DESC LIMIT 90""",
            (product_id,),
        ).fetchall()
    return [dict(r) for r in rows]
