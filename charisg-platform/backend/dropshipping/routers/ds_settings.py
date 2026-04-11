"""DS Settings — Hard Filter, 차단 브랜드/카테고리, 크롤러 파라미터."""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.dropshipping.auth import current_user
from backend.dropshipping.database import get_db

router = APIRouter(prefix="/api/ds/settings", tags=["ds-settings"])

# 메모리 캐시 (settings 테이블 미사용 — DS는 적은 설정만 보유)
_DEFAULTS = {
    "filter.real_margin_min": 25.0,
    "filter.stock_min": 10,
    "filter.price_min": 15,
    "filter.price_max": 70,
    "filter.weight_max": 2000,
    "filter.image_min": 3,
    "blocked_brands": ["Apple", "Nike", "Adidas", "Disney", "LEGO"],
    "blocked_categories": ["Health & Personal Care", "Clothing", "Shoes"],
    "crawler.delay_min": 15,
    "crawler.delay_max": 25,
    "crawler.cooldown_every": 50,
    "discord_webhook": "",
}


@router.get("/filters")
def get_filters(user: dict = Depends(current_user)):
    return {k: v for k, v in _DEFAULTS.items() if k.startswith("filter.")}


@router.put("/filters")
def put_filters(payload: dict, user: dict = Depends(current_user)):
    for k, v in payload.items():
        if k.startswith("filter."):
            _DEFAULTS[k] = v
    return {"ok": True, "filters": {k: v for k, v in _DEFAULTS.items() if k.startswith("filter.")}}


@router.get("/brands")
def get_brands(user: dict = Depends(current_user)):
    return {"blocked_brands": _DEFAULTS["blocked_brands"]}


class BrandsBody(BaseModel):
    blocked_brands: list[str]


@router.put("/brands")
def put_brands(body: BrandsBody, user: dict = Depends(current_user)):
    _DEFAULTS["blocked_brands"] = body.blocked_brands
    return {"ok": True}


@router.get("/categories")
def get_blocked_cats(user: dict = Depends(current_user)):
    return {"blocked_categories": _DEFAULTS["blocked_categories"]}


@router.get("/crawler")
def get_crawler(user: dict = Depends(current_user)):
    return {k: v for k, v in _DEFAULTS.items() if k.startswith("crawler.")}


@router.get("/discord")
def get_discord(user: dict = Depends(current_user)):
    return {"webhook": _DEFAULTS["discord_webhook"]}
