"""PA searchad — 네이버 검색광고 (월간 검색량)."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.services.naver_searchad_service import get_keyword_volumes

router = APIRouter(prefix="/api/pa/searchad", tags=["pa-searchad"])


class VolumeRequest(BaseModel):
    keywords: list[str]


@router.post("/volumes")
def fetch_volumes(req: VolumeRequest, user: dict = Depends(current_user)):
    data = get_keyword_volumes(req.keywords)
    return {"results": data or [], "note": "검색량 응답 없으면 .env NAVER_SEARCHAD_* 확인"}
