"""PA Smartstore — 네이버 스마트스토어 업로드."""
from fastapi import APIRouter, Depends, HTTPException

from backend.purchase.auth import current_user
from backend.purchase.services.smartstore_lister import list_product, build_payload

router = APIRouter(prefix="/api/pa/smartstore", tags=["pa-smartstore"])


@router.post("/upload/{product_id}")
def upload(product_id: int, user: dict = Depends(current_user)):
    return list_product(product_id)


@router.get("/preview/{product_id}")
def preview(product_id: int, user: dict = Depends(current_user)):
    payload = build_payload(product_id)
    if not payload:
        raise HTTPException(404, "상품 없음")
    return payload
