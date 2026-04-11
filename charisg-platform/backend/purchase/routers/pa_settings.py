"""PA Settings — 마진 파라미터, 크롤 스케줄, 알림, API 연동 상태."""
import os

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.database import get_db

router = APIRouter(prefix="/api/pa/settings", tags=["pa-settings"])


@router.get("")
def get_settings(user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute("SELECT key, value, updated_at FROM settings").fetchall()
    settings = {r["key"]: r["value"] for r in rows}

    integrations = {
        "naver_datalab":   bool(os.environ.get("NAVER_DATALAB_CLIENT_ID")),
        "naver_searchad":  bool(os.environ.get("NAVER_SEARCHAD_API_KEY")),
        "naver_commerce":  bool(os.environ.get("NAVER_COMMERCE_CLIENT_ID")),
        "coupang":         bool(os.environ.get("COUPANG_ACCESS_KEY")),
        "gemini":          bool(os.environ.get("GEMINI_API_KEY")),
        "discord_webhook": bool(settings.get("discord_webhook")),
    }

    return {"settings": settings, "integrations": integrations}


class SettingUpdate(BaseModel):
    key: str
    value: str


@router.put("")
def update_setting(body: SettingUpdate, user: dict = Depends(current_user)):
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO settings (key, value, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)""",
            (body.key, body.value),
        )
    return {"ok": True}
