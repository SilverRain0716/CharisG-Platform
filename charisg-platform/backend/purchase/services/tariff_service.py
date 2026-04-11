"""
tariff_service.py — 관세 자동화 (AI HS 분류 + 12,469건 관세청 DB 폴백).
"""
import json
import logging
from typing import Optional

from backend_shared.ai.service import _call_ai_async
from backend.purchase.database import get_db
from backend.purchase.services.customs_service import lookup_hs_code, estimate_duty

logger = logging.getLogger(__name__)


async def classify_hs_code(product_title: str, description: str = "") -> Optional[str]:
    """AI 로 HS 6단위 코드 추론."""
    prompt = f"""다음 상품의 HS 코드 6자리를 추론해주세요.

상품명: {product_title}
설명: {description[:300]}

JSON으로만 답변:
{{"hs6": "XXXXXX", "category_ko": "...", "confidence": 0.0~1.0}}"""

    raw = await _call_ai_async(prompt, max_tokens=300)
    try:
        data = json.loads(raw or "{}")
        return data.get("hs6")
    except (json.JSONDecodeError, TypeError):
        return None


async def get_tariff_info(product_title: str, description: str = "") -> dict:
    """AI HS 분류 → DB 조회 → 폴백 간이세율."""
    hs6 = await classify_hs_code(product_title, description)
    if hs6:
        # DB에 정확 매칭
        for length in (10, 8, 6):
            row = lookup_hs_code(hs6.ljust(length, "0")[:length])
            if row:
                return {
                    "hs_code": row["hs_code"],
                    "duty_rate": row.get("duty_rate", 8.0),
                    "vat_rate": row.get("vat_rate", 10.0),
                    "description_ko": row.get("description_ko"),
                    "source": "customs_db",
                }
    # 폴백
    return {
        "hs_code": hs6 or "unknown",
        "duty_rate": 8.0,
        "vat_rate": 10.0,
        "source": "fallback_simple_rate",
    }
