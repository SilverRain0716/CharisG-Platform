"""PA Tracking — 배송 추적 (CJ 자동 + 수동)."""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.services.tracking_service import (
    update_order_tracking, get_order_with_tracking, cj_webhook_handler,
)

router = APIRouter(prefix="/api/pa/tracking", tags=["pa-tracking"])


class TrackingUpdate(BaseModel):
    forwarder_tracking: Optional[str] = None
    domestic_tracking: Optional[str] = None
    note: str = ""


@router.patch("/{order_id}")
def update(order_id: int, body: TrackingUpdate, user: dict = Depends(current_user)):
    ok = update_order_tracking(
        order_id, body.forwarder_tracking, body.domestic_tracking, body.note,
    )
    return {"ok": ok}


@router.get("/{order_id}")
def get(order_id: int, user: dict = Depends(current_user)):
    return get_order_with_tracking(order_id) or {}


@router.post("/webhook/cj")
def cj_webhook(payload: dict):
    """CJ Open API 추적 webhook (인증 없음 — IP whitelist 권장)."""
    return cj_webhook_handler(payload)
