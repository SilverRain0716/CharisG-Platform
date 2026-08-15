"""
amazon_fee_service.py — Amazon US Referral Fee 계산
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CJ 카테고리/키워드 → Amazon 카테고리 매핑 → Referral Fee Rate 반환

마진 계산식:
  real_margin = 판매가 - (소싱가 + 배송비 + (판매가 × referral_fee_rate))
  real_margin_pct = real_margin / 판매가 × 100
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Amazon US Referral Fee 테이블 (2025-2026)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 카테고리 → (기본 수수료율, 티어 조건)
# 티어 조건: (가격 임계값, 임계값 이하 수수료, 임계값 초과 수수료) 또는 None
AMAZON_FEE_TABLE: dict[str, dict] = {
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

DEFAULT_FEE_RATE = 0.15  # 매핑 안 되면 보수적으로 15%

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CJ 키워드/카테고리 → Amazon 카테고리 매핑
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 키워드(소문자) → Amazon 카테고리
# 순서 중요: 먼저 매칭되는 것이 우선
CATEGORY_MAPPING: list[tuple[list[str], str]] = [
    # 의류/패션
    (["clothing", "apparel", "dress", "shirt", "blouse", "jacket", "coat",
      "pants", "skirt", "sweater", "hoodie", "t-shirt", "tee", "vest",
      "vintage clothing", "fashion", "ruffle", "collar", "sleeve"], "Clothing"),
    # 신발
    (["shoes", "boots", "sneakers", "sandals", "slippers", "footwear"], "Shoes"),
    # 주얼리
    (["jewelry", "necklace", "bracelet", "earring", "ring", "pendant",
      "brooch", "anklet"], "Jewelry"),
    # 시계
    (["watch", "watches", "smartwatch", "wristwatch"], "Watches"),
    # 자동차 (전자제품보다 앞에 — "car phone mount" 등에서 car 우선 매칭)
    (["automotive", "car", "vehicle", "motor", "auto"], "Automotive"),
    # 전자제품
    (["electronics", "computer", "laptop", "tablet", "phone", "camera",
      "speaker", "headphone", "earbuds", "charger", "cable"], "Electronics"),
    # 전자 악세서리 / LED
    (["led", "light", "lamp", "bulb", "strip light", "fairy light",
      "ceiling light", "sconce", "strobe", "neon", "electronics accessory",
      "phone case", "screen protector"], "Electronics Acc."),
    # 뷰티
    (["beauty", "makeup", "cosmetic", "skincare", "lipstick", "mascara",
      "vanity", "mirror vanity"], "Beauty"),
    # 반려동물
    (["pet", "dog", "cat", "puppy", "kitten", "aquarium", "fish tank",
      "bird", "hamster"], "Pet Supplies"),
    # 장난감
    (["toy", "toys", "game", "puzzle", "building blocks", "lego", "doll",
      "action figure", "miniature", "plush"], "Toys & Games"),
    # 스포츠
    (["sports", "fitness", "gym", "yoga", "exercise", "camping", "hiking",
      "bicycle", "cycling"], "Sports & Outdoors"),
    # 정원/아웃도어
    (["garden", "outdoor", "patio", "plant pot", "flower pot", "planter",
      "lawn", "sprinkler"], "Garden & Outdoor"),
    # 가구
    (["furniture", "shelf", "shelves", "cabinet", "desk", "chair", "table",
      "bookshelf", "drawer", "wardrobe", "sofa", "couch", "bed frame"], "Furniture"),
    # 건강
    (["health", "vitamin", "supplement", "medical", "first aid",
      "thermometer"], "Health & Household"),
    # 베이비
    (["baby", "infant", "toddler", "stroller", "diaper", "nursery"], "Baby Products"),
    # 도구
    (["tool", "drill", "wrench", "screwdriver", "hardware",
      "home improvement"], "Tools & Home"),
    # 홈 & 키친 (가장 넓은 범위 — 마지막에 배치)
    (["home", "kitchen", "decor", "wall art", "candle", "vase", "cushion",
      "pillow", "curtain", "rug", "mat", "towel", "storage", "organizer",
      "clock", "frame", "decoration", "gothic", "halloween", "christmas",
      "artificial plant", "wedding", "party", "blender", "mixer", "utensil",
      "cookware", "bakeware", "cutting board"], "Home & Kitchen"),
]


def get_referral_fee_rate(
    category: str = "",
    product_name: str = "",
) -> float:
    """
    CJ 카테고리/상품명 → Amazon Referral Fee Rate 반환

    1. 카테고리 키워드 매칭 시도
    2. 상품명 키워드 매칭 시도
    3. 매칭 안 되면 디폴트 15%
    """
    amazon_cat = _map_to_amazon_category(category, product_name)
    fee_info = AMAZON_FEE_TABLE.get(amazon_cat, {"rate": DEFAULT_FEE_RATE, "tier": None})
    return fee_info["rate"]


def get_referral_fee_amount(
    sale_price: float,
    category: str = "",
    product_name: str = "",
) -> float:
    """
    실제 Referral Fee 금액 계산 (티어 구조 반영)

    Returns: Referral Fee 금액 ($)
    """
    amazon_cat = _map_to_amazon_category(category, product_name)
    fee_info = AMAZON_FEE_TABLE.get(amazon_cat, {"rate": DEFAULT_FEE_RATE, "tier": None})

    tier = fee_info.get("tier")
    if tier and sale_price > 0:
        threshold = tier["threshold"]
        if sale_price <= threshold:
            fee = sale_price * tier["below"]
        else:
            fee = threshold * tier["below"] + (sale_price - threshold) * tier["above"]
    else:
        fee = sale_price * fee_info["rate"]

    # 최소 referral fee: $0.30
    return max(fee, 0.30) if sale_price > 0 else 0


def get_amazon_category(
    category: str = "",
    product_name: str = "",
) -> str:
    """CJ 카테고리/상품명 → Amazon 카테고리명 반환"""
    return _map_to_amazon_category(category, product_name)


def calc_real_margin(
    source_price: float,
    ship_cost: float,
    sale_price: float,
    category: str = "",
    product_name: str = "",
) -> float:
    """
    Amazon 수수료 반영 실질 마진율 계산

    real_margin = 판매가 - (소싱가 + 배송비 + referral_fee)
    real_margin_pct = real_margin / 판매가 × 100
    """
    if sale_price <= 0:
        return 0.0

    referral_fee = get_referral_fee_amount(sale_price, category, product_name)
    real_margin = sale_price - (source_price + ship_cost + referral_fee)
    return round(real_margin / sale_price * 100, 2)


def _map_to_amazon_category(category: str, product_name: str) -> str:
    """CJ 키워드 → Amazon 카테고리 매핑 (키워드 매칭)

    우선순위: 카테고리 문자열 먼저, 그 다음 상품명에서 매칭
    """
    cat_lower = category.lower()
    name_lower = product_name.lower()

    # 1차: 카테고리 키워드로 매칭 (가장 신뢰도 높음)
    for keywords, amazon_cat in CATEGORY_MAPPING:
        for kw in keywords:
            if kw in cat_lower:
                return amazon_cat

    # 2차: 상품명에서 매칭
    for keywords, amazon_cat in CATEGORY_MAPPING:
        for kw in keywords:
            if kw in name_lower:
                return amazon_cat

    return "Everything Else"
