"""
scoring_service.py (PA) — 2축 + 통관 필터 스코어링.

DS와 다름:
  DS: Demand × Gap × Margin (3축 곱셈) + Hard Filter 8개
  PA: Demand × Margin (2축) + 통관 리스크 필터 (별도, 곱셈 아님)
"""
from typing import Optional

from backend.purchase.database import get_db


def grade_demand(monthly_volume: int) -> str:
    if monthly_volume >= 10000:
        return "A"
    if monthly_volume >= 1000:
        return "B"
    return "C"


def grade_margin(margin_pct: float) -> str:
    if margin_pct >= 30:
        return "A"
    if margin_pct >= 15:
        return "B"
    return "C"


def calculate(monthly_volume: int, margin_pct: float, customs_risk: str) -> dict:
    """Returns: {demand_grade, margin_grade, matrix, score, eligible}"""
    d = grade_demand(monthly_volume)
    m = grade_margin(margin_pct)
    matrix = d + m
    # 통관 REJECT 면 즉시 탈락
    eligible = customs_risk != "REJECT"
    # 0~1 정규화
    d_score = {"A": 1.0, "B": 0.6, "C": 0.3}[d]
    m_score = {"A": 1.0, "B": 0.6, "C": 0.3}[m]
    score = round(d_score * m_score, 3)
    return {
        "demand_grade": d,
        "margin_grade": m,
        "matrix": matrix,
        "score": score,
        "eligible": eligible,
    }
