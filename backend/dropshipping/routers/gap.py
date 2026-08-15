"""DS Gap — Gap Score 데이터 (amazon_search_agg) 조회 + 재계산."""
from fastapi import APIRouter, Depends, HTTPException

from backend.dropshipping.auth import current_user
from backend.dropshipping.database import get_db

router = APIRouter(prefix="/api/ds/gap", tags=["ds-gap"])


@router.get("/keywords")
def list_gap_keywords(user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT keyword, price_p75, price_max, avg_review_count,
                      min_review_count, fbm_count, total_results, collected_at
               FROM amazon_search_agg ORDER BY collected_at DESC LIMIT 200"""
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/keyword/{keyword}")
def get_gap_keyword(keyword: str, user: dict = Depends(current_user)):
    with get_db() as conn:
        agg = conn.execute("SELECT * FROM amazon_search_agg WHERE keyword=?", (keyword,)).fetchone()
        if not agg:
            raise HTTPException(404, "키워드 데이터 없음")
        items = conn.execute(
            "SELECT * FROM amazon_search_results WHERE keyword=? ORDER BY price ASC LIMIT 50",
            (keyword,),
        ).fetchall()
    return {"agg": dict(agg), "results": [dict(r) for r in items]}
