"""
competition_service.py — 경쟁 가격 비교.

내 판매가 vs 쿠팡/네이버 검색 결과 → 경쟁력 등급 (HIGH/MED/LOW).
실제 크롤링 부분은 EC2 deploy 후 활성화 — 여기서는 인터페이스 + 등급 산정.
"""
from typing import Optional

from backend.purchase.database import get_db


def grade_competition(my_price: float, competitor_prices: list[float]) -> str:
    """경쟁가 대비 우리 판매가 등급.
    HIGH: 경쟁가의 90% 이하 (우리 가격 경쟁력 높음)
    MED:  90~110%
    LOW:  110% 이상
    """
    if not competitor_prices:
        return "MED"
    avg_comp = sum(competitor_prices) / len(competitor_prices)
    ratio = my_price / avg_comp
    if ratio <= 0.90:
        return "HIGH"
    if ratio <= 1.10:
        return "MED"
    return "LOW"


def store_snapshot(product_id: int, channel: str, price: float, rank: int = None) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO competition_snapshots (product_id, competitor_channel, competitor_price, rank)
               VALUES (?, ?, ?, ?)""",
            (product_id, channel, price, rank),
        )
    return cur.lastrowid


def get_recent_snapshots(product_id: int, limit: int = 30) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT competitor_channel, competitor_price, rank, captured_at
               FROM competition_snapshots
               WHERE product_id=? ORDER BY captured_at DESC LIMIT ?""",
            (product_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
