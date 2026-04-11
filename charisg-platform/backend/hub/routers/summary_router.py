"""
Hub summary router — /api/hub/summary

Hub 대시보드 요약: DS + PA 양쪽 KPI를 fan-out 호출.
DS/PA API가 죽어 있어도 카드는 빈 값으로 표시 (UI 깨지지 않음).
"""
import asyncio
import logging
import os
from typing import Any, Dict

import httpx
from fastapi import APIRouter, Depends

from backend.hub.auth import current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/hub", tags=["hub-summary"])

DS_API_BASE = os.environ.get("DS_API_BASE", "http://127.0.0.1:8001")
PA_API_BASE = os.environ.get("PA_API_BASE", "http://127.0.0.1:8002")


async def _fetch(client: httpx.AsyncClient, url: str) -> Dict[str, Any] | None:
    try:
        r = await client.get(url, timeout=3.0)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.warning(f"summary fetch 실패: {url} → {e}")
    return None


@router.get("/summary")
async def get_summary(user: dict = Depends(current_user)):
    """양쪽 앱 요약 KPI를 모아서 반환. 빈 값은 None 으로."""
    async with httpx.AsyncClient() as client:
        ds_task = _fetch(client, f"{DS_API_BASE}/api/ds/summary")
        pa_task = _fetch(client, f"{PA_API_BASE}/api/pa/summary")
        ds, pa = await asyncio.gather(ds_task, pa_task)

    return {
        "ds": ds or {
            "active_products": None,
            "total_revenue": None,
            "avg_margin": None,
            "go_count": None,
            "pendingCount": None,
            "kpis": [],
        },
        "pa": pa or {
            "active_products": None,
            "monthly_revenue": None,
            "avg_margin": None,
            "pending_orders": None,
            "pendingCount": None,
            "kpis": [],
        },
    }
