"""
customs_service.py — 통관 리스크 체크.

목록통관 vs 일반통관 자동 태그.
관세청 DB(tariff_codes) 12,469건 조회 — 없는 HS코드는 AI HS 분류 + 간이세율 폴백.
"""
import logging
from typing import Optional

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)

# 목록통관 한도 (1회 $150 USD or 200 USD 미국 직구 — 정책 변동 가능)
LIST_CLEARANCE_USD_LIMIT = 150.0

# 통관 차단 카테고리 (식품/의약품/위험물)
BLOCKED_KEYWORDS = (
    "supplement", "vitamin", "drug", "medicine",
    "battery lithium", "perfume", "alcohol",
    "knife", "weapon", "gun", "ammo",
)


def quick_check(amazon_price_usd: float, title: str = "") -> dict:
    """빠른 통관 가능성 체크 (AI 호출 없음)."""
    title_l = (title or "").lower()
    for kw in BLOCKED_KEYWORDS:
        if kw in title_l:
            return {
                "risk": "REJECT",
                "reason": f"차단 키워드: {kw}",
                "classification": "일반통관",
            }
    if amazon_price_usd <= LIST_CLEARANCE_USD_LIMIT:
        return {
            "risk": "PASS",
            "reason": f"${LIST_CLEARANCE_USD_LIMIT} 이하 — 목록통관 가능",
            "classification": "목록통관",
        }
    return {
        "risk": "WARN",
        "reason": f"${LIST_CLEARANCE_USD_LIMIT} 초과 — 일반통관 (관부가세 발생)",
        "classification": "일반통관",
    }


def lookup_hs_code(hs_code: str) -> Optional[dict]:
    """관세청 DB 조회."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tariff_codes WHERE hs_code=?", (hs_code,),
        ).fetchone()
    return dict(row) if row else None


def estimate_duty(amazon_price_usd: float, fx_rate: float, duty_rate: float = 8.0,
                  vat_rate: float = 10.0) -> float:
    """관부가세 계산 (간이세율 디폴트 8%, VAT 10%)."""
    cif_krw = amazon_price_usd * fx_rate
    duty = cif_krw * (duty_rate / 100.0)
    vat = (cif_krw + duty) * (vat_rate / 100.0)
    return round(duty + vat, 0)


def save_check(sourcing_id: int, check: dict, hs_code: str = None) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO customs_checks
               (sourcing_id, hs_code, hs_source, classification, duty_rate, risk, risk_reason)
               VALUES (?, ?, 'manual', ?, ?, ?, ?)""",
            (sourcing_id, hs_code, check.get("classification"),
             check.get("duty_rate", 8.0), check.get("risk"), check.get("reason")),
        )
    return cur.lastrowid
