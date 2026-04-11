"""DS Category — 카테고리 매핑 + 동기화."""
from fastapi import APIRouter, Depends

from backend.dropshipping.auth import current_user
from backend.dropshipping.services.amazon_fee_service import get_amazon_category

router = APIRouter(prefix="/api/ds/category", tags=["ds-category"])


@router.get("/match")
def match(category: str = "", product_name: str = "", user: dict = Depends(current_user)):
    return {
        "input": {"category": category, "product_name": product_name},
        "amazon_category": get_amazon_category(category, product_name),
    }
