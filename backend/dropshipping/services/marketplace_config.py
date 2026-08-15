"""
marketplace_config.py — 마켓플레이스별 설정 중앙 관리.

모든 마켓별 차이(환율, 수수료, 배송비, 제한 카테고리, lead_time)를 여기서 관리.
서비스 코드에서는 get_config(market) 으로 가져다 씀.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Amazon Referral Fee 테이블 (마켓별)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# US 수수료 (기존 amazon_fee_service.py 에서 이관)
_FEE_TABLE_US = {
    "Home & Kitchen":       {"rate": 0.15, "tier": None},
    "Garden & Outdoor":     {"rate": 0.15, "tier": None},
    "Furniture":            {"rate": 0.15, "tier": None},
    "Pet Supplies":         {"rate": 0.15, "tier": None},
    "Toys & Games":         {"rate": 0.15, "tier": None},
    "Sports & Outdoors":    {"rate": 0.15, "tier": None},
    "Tools & Home":         {"rate": 0.15, "tier": None},
    "Office Products":      {"rate": 0.15, "tier": None},
    "Everything Else":      {"rate": 0.15, "tier": None},
    "Clothing":             {"rate": 0.17, "tier": None},
    "Shoes":                {"rate": 0.15, "tier": None},
    "Jewelry":              {"rate": 0.20, "tier": {"threshold": 250, "below": 0.20, "above": 0.05}},
    "Watches":              {"rate": 0.16, "tier": {"threshold": 1500, "below": 0.16, "above": 0.03}},
    "Electronics":          {"rate": 0.08, "tier": None},
    "Electronics Acc.":     {"rate": 0.15, "tier": {"threshold": 100, "below": 0.15, "above": 0.08}},
    "Automotive":           {"rate": 0.12, "tier": None},
    "Beauty":               {"rate": 0.15, "tier": {"threshold": 10, "below": 0.08, "above": 0.15}},
    "Baby Products":        {"rate": 0.15, "tier": {"threshold": 10, "below": 0.08, "above": 0.15}},
    "Health & Household":   {"rate": 0.15, "tier": {"threshold": 10, "below": 0.08, "above": 0.15}},
    "Grocery":              {"rate": 0.15, "tier": {"threshold": 15, "below": 0.08, "above": 0.15}},
}

# Canada 수수료 (US와 유사, 일부 차이)
_FEE_TABLE_CA = {
    "Home & Kitchen":       {"rate": 0.15, "tier": None},
    "Garden & Outdoor":     {"rate": 0.15, "tier": None},
    "Furniture":            {"rate": 0.15, "tier": None},
    "Pet Supplies":         {"rate": 0.15, "tier": None},
    "Toys & Games":         {"rate": 0.15, "tier": None},
    "Sports & Outdoors":    {"rate": 0.15, "tier": None},
    "Tools & Home":         {"rate": 0.15, "tier": None},
    "Office Products":      {"rate": 0.15, "tier": None},
    "Everything Else":      {"rate": 0.15, "tier": None},
    "Clothing":             {"rate": 0.15, "tier": None},  # CA: 15% (US 17%)
    "Shoes":                {"rate": 0.15, "tier": None},
    "Jewelry":              {"rate": 0.20, "tier": {"threshold": 250, "below": 0.20, "above": 0.05}},
    "Watches":              {"rate": 0.16, "tier": {"threshold": 1500, "below": 0.16, "above": 0.03}},
    "Electronics":          {"rate": 0.08, "tier": None},
    "Electronics Acc.":     {"rate": 0.15, "tier": {"threshold": 100, "below": 0.15, "above": 0.08}},
    "Automotive":           {"rate": 0.12, "tier": None},
    "Beauty":               {"rate": 0.15, "tier": {"threshold": 10, "below": 0.08, "above": 0.15}},
    "Baby Products":        {"rate": 0.15, "tier": {"threshold": 10, "below": 0.08, "above": 0.15}},
    "Health & Household":   {"rate": 0.15, "tier": {"threshold": 10, "below": 0.08, "above": 0.15}},
    "Grocery":              {"rate": 0.15, "tier": {"threshold": 15, "below": 0.08, "above": 0.15}},
}

# Mexico 수수료
_FEE_TABLE_MX = {
    "Home & Kitchen":       {"rate": 0.15, "tier": None},
    "Garden & Outdoor":     {"rate": 0.15, "tier": None},
    "Furniture":            {"rate": 0.15, "tier": None},
    "Pet Supplies":         {"rate": 0.15, "tier": None},
    "Toys & Games":         {"rate": 0.15, "tier": None},
    "Sports & Outdoors":    {"rate": 0.15, "tier": None},
    "Tools & Home":         {"rate": 0.15, "tier": None},
    "Office Products":      {"rate": 0.15, "tier": None},
    "Everything Else":      {"rate": 0.15, "tier": None},
    "Clothing":             {"rate": 0.15, "tier": None},
    "Shoes":                {"rate": 0.15, "tier": None},
    "Jewelry":              {"rate": 0.15, "tier": None},  # MX: 15% (US 20%)
    "Watches":              {"rate": 0.15, "tier": None},
    "Electronics":          {"rate": 0.10, "tier": None},  # MX: 10% (US 8%)
    "Electronics Acc.":     {"rate": 0.15, "tier": None},
    "Automotive":           {"rate": 0.12, "tier": None},
    "Beauty":               {"rate": 0.15, "tier": None},
    "Baby Products":        {"rate": 0.15, "tier": None},
    "Health & Household":   {"rate": 0.15, "tier": None},
    "Grocery":              {"rate": 0.10, "tier": None},
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 제한 카테고리 (마켓별)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_RESTRICTED_US = [
    "pesticide", "insecticide", "bug killer", "pest control",
    "mosquito killer", "roach killer", "rat poison", "insect killer",
    "pest repell", "supplement", "medication", "pharmaceutical",
    "anti wrinkle", "collagen mask", "face serum",
    "skincare set", "skin care set",
    "tens unit", "muscle stimulator", "medical device", "respirator mask",
    "torch lighter", "propane torch", "cigar torch", "welding gun",
    "memory card", "micro sd card", "sd card",
    "drone", "quadcopter", "smart ring health",
    "energy strip", "caffeine strip",
]

_RESTRICTED_CA = [
    # Canada: US와 유사 + Health Canada 규정
    "pesticide", "insecticide", "bug killer", "pest control",
    "mosquito killer", "roach killer", "rat poison", "insect killer",
    "pest repell", "supplement", "medication", "pharmaceutical",
    "anti wrinkle", "collagen mask", "face serum",
    "skincare set", "skin care set",
    "tens unit", "muscle stimulator", "medical device", "respirator mask",
    "torch lighter", "propane torch", "cigar torch", "welding gun",
    "memory card", "micro sd card", "sd card",
    "drone", "quadcopter", "smart ring health",
    "energy strip", "caffeine strip",
    # Canada 추가 규정
    "cannabis", "vape", "e-cigarette",
]

_RESTRICTED_MX = [
    # Mexico: 규제 적음, 핵심만
    "pesticide", "insecticide", "pest control",
    "medication", "pharmaceutical",
    "medical device", "respirator mask",
    "torch lighter", "propane torch", "welding gun",
    "drone", "quadcopter",
    "memory card", "micro sd card", "sd card",
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 메인 설정
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MARKETPLACE_CONFIG = {
    "US": {
        "marketplace_id": "ATVPDKIKX0DER",
        "country_code": "US",
        "currency": "USD",
        "exchange_rate": 1.0,
        "lead_time_us_wh": 5,
        "lead_time_cn_wh": 18,
        "min_sale_price_local": 15.0,
        "min_margin_pct": 25,
        "min_margin_mult": 1.40,
        "default_ship_cost_us": 4.99,
        "default_ship_cost_cn": 5.99,
        "fee_table": _FEE_TABLE_US,
        "restricted_phrases": _RESTRICTED_US,
        "locale": "en_US",
        "sku_prefix": "CG-DS-US",
    },
    "CA": {
        "marketplace_id": "A2EUQ1WTGCTBG2",
        "country_code": "CA",
        "currency": "CAD",
        "exchange_rate": 1.37,
        "lead_time_us_wh": 7,
        "lead_time_cn_wh": 15,
        "min_sale_price_local": 20.0,
        "min_margin_pct": 25,
        "min_margin_mult": 1.40,
        "default_ship_cost_us": 5.99,
        "default_ship_cost_cn": 5.99,
        "fee_table": _FEE_TABLE_CA,
        "restricted_phrases": _RESTRICTED_CA,
        "locale": "en_CA",
        "sku_prefix": "CG-DS-CA",
    },
    "MX": {
        "marketplace_id": "A1AM78C64UM0Y8",
        "country_code": "MX",
        "currency": "MXN",
        "exchange_rate": 17.0,
        "lead_time_us_wh": 10,
        "lead_time_cn_wh": 20,
        "min_sale_price_local": 250.0,
        "min_margin_pct": 20,
        "min_margin_mult": 1.35,
        "default_ship_cost_us": 8.00,
        "default_ship_cost_cn": 8.00,
        "fee_table": _FEE_TABLE_MX,
        "restricted_phrases": _RESTRICTED_MX,
        "locale": "es_MX",
        "sku_prefix": "CG-DS-MX",
    },
}

ALL_MARKETS = list(MARKETPLACE_CONFIG.keys())


def get_config(market: str = "US") -> dict:
    """마켓 설정 반환. 잘못된 마켓이면 US 기본값."""
    cfg = MARKETPLACE_CONFIG.get(market.upper())
    if not cfg:
        logger.warning(f"알 수 없는 마켓 '{market}', US 기본값 사용")
        cfg = MARKETPLACE_CONFIG["US"]
    return cfg


def get_lead_time(market: str, warehouse_country: str) -> int:
    """마켓 + 창고 조합으로 lead_time 반환."""
    cfg = get_config(market)
    if warehouse_country == "US":
        return cfg["lead_time_us_wh"]
    return cfg["lead_time_cn_wh"]


def get_default_ship_cost(market: str, warehouse_country: str) -> float:
    """마켓 + 창고 조합으로 기본 배송비 반환."""
    cfg = get_config(market)
    if warehouse_country == "US":
        return cfg["default_ship_cost_us"]
    return cfg["default_ship_cost_cn"]


def usd_to_local(usd_amount: float, market: str) -> float:
    """USD → 현지 통화 변환."""
    cfg = get_config(market)
    return round(usd_amount * cfg["exchange_rate"], 2)


def local_to_usd(local_amount: float, market: str) -> float:
    """현지 통화 → USD 변환."""
    cfg = get_config(market)
    rate = cfg["exchange_rate"]
    return round(local_amount / rate, 2) if rate > 0 else 0.0


def make_sku(product_id: int, market: str = "US") -> str:
    """마켓별 SKU 생성."""
    cfg = get_config(market)
    return f"{cfg['sku_prefix']}-{product_id}"
