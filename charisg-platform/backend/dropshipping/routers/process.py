"""DS Process — Bulk 처리 (번역, SEO, 이미지)."""
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel

from backend.dropshipping.auth import current_user
from backend.dropshipping.database import get_db
from backend_shared.ai import translate_text

router = APIRouter(prefix="/api/ds/process", tags=["ds-process"])


class BulkRequest(BaseModel):
    product_ids: list[int]
    operation: str  # 'translate' | 'seo' | 'image'


@router.post("/bulk")
async def bulk_process(req: BulkRequest, user: dict = Depends(current_user)):
    """단일 op 일괄 처리. AI rate limit 으로 순차 실행."""
    if req.operation not in {"translate", "seo", "image"}:
        return {"ok": False, "error": "unsupported operation"}

    with get_db() as conn:
        rows = conn.execute(
            f"SELECT id, product_name FROM collected_products WHERE id IN ({','.join(['?']*len(req.product_ids))})",
            tuple(req.product_ids),
        ).fetchall()

    results = []
    if req.operation == "translate":
        for r in rows:
            translated = await translate_text(r["product_name"], "en", "ko")
            results.append({"id": r["id"], "translated": translated.get("translated")})

    return {"ok": True, "operation": req.operation, "count": len(results), "results": results}
