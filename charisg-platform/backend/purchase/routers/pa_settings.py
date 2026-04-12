"""PA Settings — 마진 파라미터, 크롤 스케줄, 알림, API 연동 상태."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend_shared import _config as cfg

router = APIRouter(prefix="/api/pa/settings", tags=["pa-settings"])


@router.get("")
def get_settings(user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute("SELECT key, value, updated_at FROM settings").fetchall()
    settings = {r["key"]: r["value"] for r in rows}

    # backend_shared._config 에서 fallback 처리된 값을 사용 (모노리스 NAVER_CLIENT_ID 호환).
    integrations = {
        "naver_datalab":   bool(cfg.NAVER_DATALAB_CLIENT_ID),
        "naver_searchad":  bool(cfg.NAVER_SEARCHAD_API_KEY),
        "naver_commerce":  bool(cfg.NAVER_COMMERCE_CLIENT_ID),
        "coupang":         bool(cfg.COUPANG_ACCESS_KEY),
        "gemini":          bool(cfg.GEMINI_API_KEY),
        "github":          bool(cfg.GITHUB_TOKEN),
        "cj":              bool(cfg.CJ_API_KEY),
        "webshare_proxy":  bool(cfg.PROXY_HOST and cfg.PROXY_USER_BASE),
        "discord_webhook": bool(cfg.DISCORD_WEBHOOK_URL or settings.get("discord_webhook")),
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


# ──────────────────────────────────────────────
# Pricing settings (환율 + 마진 + 수수료 + 배송비 + 이미지 보관)
# ──────────────────────────────────────────────

_PRICING_FLOAT_KEYS = (
    "margin_target_rate",
    "smartstore_fee_rate",
    "coupang_fee_rate",
    "amazon_shipping_default_usd",
    "cj_shipping_default_usd_per_kg",
    "exchange_rate_usd_krw",
)
_PRICING_INT_KEYS = ("image_retention_days",)
_PRICING_STR_KEYS = ("exchange_rate_updated_at",)
# 환율 2필드는 GET 에서만 노출, PUT 에서는 UpdatePricingBody 에 부재 → extra=forbid 로 422
_PRICING_READONLY_KEYS = ("exchange_rate_usd_krw", "exchange_rate_updated_at")


def _load_pricing_settings() -> dict:
    keys = _PRICING_FLOAT_KEYS + _PRICING_INT_KEYS + _PRICING_STR_KEYS
    placeholders = ",".join("?" * len(keys))
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT key, value FROM settings WHERE key IN ({placeholders})",
            keys,
        ).fetchall()
    raw = {r["key"]: r["value"] for r in rows}
    out: dict = {}
    for k in _PRICING_FLOAT_KEYS:
        v = raw.get(k)
        out[k] = float(v) if v not in (None, "") else None
    for k in _PRICING_INT_KEYS:
        v = raw.get(k)
        out[k] = int(v) if v not in (None, "") else None
    for k in _PRICING_STR_KEYS:
        out[k] = raw.get(k, "") or ""
    return out


class UpdatePricingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    margin_target_rate: float | None = Field(default=None, gt=0, lt=0.95)
    smartstore_fee_rate: float | None = Field(default=None, ge=0, lt=0.5)
    coupang_fee_rate: float | None = Field(default=None, ge=0, lt=0.5)
    amazon_shipping_default_usd: float | None = Field(default=None, ge=0)
    cj_shipping_default_usd_per_kg: float | None = Field(default=None, ge=0)
    image_retention_days: int | None = Field(default=None, ge=1, le=365)


@router.get("/pricing")
def get_pricing_settings(user: dict = Depends(current_user)):
    return _load_pricing_settings()


@router.put("/pricing")
def update_pricing_settings(
    body: UpdatePricingBody, user: dict = Depends(current_user)
):
    updates = body.model_dump(exclude_none=True)
    if updates:
        with get_db() as conn:
            for key, value in updates.items():
                conn.execute(
                    """INSERT OR REPLACE INTO settings (key, value, updated_at)
                       VALUES (?, ?, CURRENT_TIMESTAMP)""",
                    (key, str(value)),
                )
    return _load_pricing_settings()
