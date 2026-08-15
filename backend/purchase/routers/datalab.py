"""PA datalab — 네이버 데이터랩 (카테고리 트렌드 조회 전용).

풀 파이프라인 실행은 /api/pa/discovery/run 으로 이동됨.
"""
from fastapi import APIRouter, Depends

from backend.purchase.auth import current_user
from backend.purchase.services import naver_datalab_service

router = APIRouter(prefix="/api/pa/datalab", tags=["pa-datalab"])


@router.get("/trends")
def get_trends(category_param: str = "50000000", days: int = 30, user: dict = Depends(current_user)):
    data = naver_datalab_service.fetch_category_trends("전체", category_param, days)
    return {"data": data or [], "note": "데이터랩 응답 없으면 .env NAVER_DATALAB_* 확인"}
