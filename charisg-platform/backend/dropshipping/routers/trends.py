"""DS Trends — Google Trends 기반 키워드 신호."""
from fastapi import APIRouter, Depends

from backend.dropshipping.auth import current_user

router = APIRouter(prefix="/api/ds/trends", tags=["ds-trends"])


@router.get("/categories")
def get_categories(user: dict = Depends(current_user)):
    """카테고리별 데맨드 점수 (수동 매핑)."""
    from backend.dropshipping.services.scoring_service import CATEGORY_DEMAND
    return [{"category": k, "demand": v} for k, v in CATEGORY_DEMAND.items()]


@router.get("/keyword/{keyword}")
def get_keyword_trend(keyword: str, user: dict = Depends(current_user)):
    """Google Trends 조회 — pytrends 무료 티어, 실패 시 fallback 0.4."""
    from backend.dropshipping.services.scoring_service import _get_trend_score
    score = _get_trend_score(keyword)
    return {"keyword": keyword, "score": score}
