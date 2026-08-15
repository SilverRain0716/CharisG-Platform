"""PA Settings — 마진 파라미터, 크롤 스케줄, 알림, API 연동 상태."""
import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend_shared import _config as cfg

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pa/settings", tags=["pa-settings"])

# 네이버 RELEASE 주소록 TTL 캐시 (배대지 주소 바뀔 일 드물어 1시간 충분).
_RELEASE_ADDRESS_CACHE: dict = {"fetched_at": 0, "data": None}
_RELEASE_TTL_SEC = 3600


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


# ──────────────────────────────────────────────
# Release address (배대지 주소) — Naver addressbook RELEASE 조회
# 아마존 발주 패널 "Ship to (forwarder)" 표시용. TTL 1h 캐시.
# ──────────────────────────────────────────────

def _format_release_address(entry: dict) -> dict:
    """Naver addressbook entry → 패널에서 쓰기 좋은 평탄화된 dict.

    해외주소(overseasAddress=True)의 경우 `address` 필드에 전체 완성형이 들어있고
    base/detail은 일부 조각만 — Ship-To 복사용 full_line은 `address` 원본을 우선 사용.
    """
    if not isinstance(entry, dict):
        return {}
    base = entry.get("baseAddress", "") or ""
    detail = entry.get("detailAddress", "") or ""
    postal = entry.get("postalCode", "") or ""
    phone1 = entry.get("phoneNumber1", "") or ""
    phone2 = entry.get("phoneNumber2", "") or ""
    oversea = bool(entry.get("overseasAddress"))
    raw_address = entry.get("address", "") or ""

    # Ship-To 복사용 multi-line 포맷
    # - 해외주소: name + 원본 address (도시/주/국가 포함) + phone
    # - 국내주소: name + base + detail + postal + phone
    if oversea:
        full_line = "\n".join(
            p for p in (entry.get("name", ""), raw_address, f"Phone: {phone1}" if phone1 else "") if p
        )
    else:
        full_line = "\n".join(
            p for p in (entry.get("name", ""), base, detail, postal, f"Tel: {phone1}" if phone1 else "") if p
        )

    return {
        "name": entry.get("name", ""),
        "base_address": base,
        "detail_address": detail,
        "postal_code": postal,
        "phone1": phone1,
        "phone2": phone2,
        "oversea": oversea,
        "raw_address": raw_address,
        "full_line": full_line,
    }


@router.get("/release-address")
def get_release_address(user: dict = Depends(current_user)):
    """배대지(네이버 RELEASE 주소록 1번 엔트리) — 아마존 발주 ship-to 용.

    1시간 TTL 캐시. 조회 실패 시 503.
    """
    now = time.time()
    if _RELEASE_ADDRESS_CACHE["data"] and (now - _RELEASE_ADDRESS_CACHE["fetched_at"]) < _RELEASE_TTL_SEC:
        return _RELEASE_ADDRESS_CACHE["data"]

    # 지연 import — 모듈 import 시점에 네이버 설정 없어도 부팅 되게.
    from backend.purchase.services.naver_commerce_service import get_addressbook_by_type

    entry = get_addressbook_by_type("RELEASE")
    if not entry:
        raise HTTPException(503, "네이버 주소록 조회 실패 — naver_commerce 인증 확인")
    data = _format_release_address(entry)
    _RELEASE_ADDRESS_CACHE["data"] = data
    _RELEASE_ADDRESS_CACHE["fetched_at"] = now
    return data


@router.post("/release-address/refresh")
def refresh_release_address(user: dict = Depends(current_user)):
    """캐시 무효화 후 재조회."""
    _RELEASE_ADDRESS_CACHE["data"] = None
    _RELEASE_ADDRESS_CACHE["fetched_at"] = 0
    return get_release_address(user)
