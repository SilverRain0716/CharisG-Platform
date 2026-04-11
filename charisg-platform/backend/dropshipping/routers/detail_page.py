"""DS Detail Page — Amazon Tier 1/2 리스팅 콘텐츠 생성 (AI)."""
from fastapi import APIRouter, Depends, HTTPException

from backend.dropshipping.auth import current_user
from backend.dropshipping.database import get_db
from backend_shared.ai import translate_text, generate_seo

router = APIRouter(prefix="/api/ds/detail-page", tags=["ds-detail-page"])


@router.post("/{product_id}/generate")
async def generate_detail(product_id: int, user: dict = Depends(current_user)):
    with get_db() as conn:
        row = conn.execute(
            "SELECT product_name, category FROM collected_products WHERE id=?",
            (product_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "상품 없음")

    seo = await generate_seo(
        product_name=row["product_name"],
        category=row["category"] or "",
        market="US",
        platform="amazon",
    )
    return {"product_id": product_id, "seo": seo}


@router.post("/translate")
async def translate(body: dict, user: dict = Depends(current_user)):
    text = body.get("text", "")
    target = body.get("target_lang", "en")
    return await translate_text(text, source_lang="ko", target_lang=target)
