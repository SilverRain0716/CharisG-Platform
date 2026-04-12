"""PA Detail Page — AI 번역 + SEO + 상세페이지 HTML 생성."""
import json
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services.ai_processor import process_product, process_batch

router = APIRouter(prefix="/api/pa/detail-page", tags=["pa-detail-page"])


# ── 단일 생성 ──────────────────────────────────

@router.post("/{product_id}/generate")
async def generate(product_id: int, user: dict = Depends(current_user)):
    try:
        result = await process_product(product_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI 처리 실패: {e}")
    return result


# ── SSE 일괄 생성 ──────────────────────────────

class BatchBody(BaseModel):
    product_ids: list[int] | None = None
    all_unprocessed: bool = False
    platform: str = "smartstore"


@router.post("/batch")
async def batch_generate(body: BatchBody, user: dict = Depends(current_user)):
    if body.all_unprocessed:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id FROM products WHERE ai_processed_at IS NULL ORDER BY id"
            ).fetchall()
        product_ids = [r["id"] for r in rows]
    elif body.product_ids:
        product_ids = body.product_ids
    else:
        raise HTTPException(400, "product_ids 또는 all_unprocessed=true 필요")

    if not product_ids:
        raise HTTPException(400, "처리 대상 상품 없음")

    async def event_stream():
        t0 = time.time()
        async for item in process_batch(product_ids, body.platform):
            if item.get("event") == "done":
                item["elapsed_sec"] = round(time.time() - t0, 1)
                yield f"event: done\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
            else:
                yield f"event: progress\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── 조회 ──────────────────────────────────────

@router.get("/{product_id}")
def get_detail_page(product_id: int, user: dict = Depends(current_user)):
    with get_db() as conn:
        row = conn.execute(
            """SELECT id, product_id, html_content, sections, market, platform,
                      status, created_at, updated_at
               FROM detail_pages WHERE product_id=?
               ORDER BY updated_at DESC LIMIT 1""",
            (product_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "상세페이지 없음")
    return dict(row)


# ── 수동 편집 ─────────────────────────────────

class UpdateSection(BaseModel):
    sections: list | None = None
    html_content: str | None = None


@router.put("/{product_id}")
def update(product_id: int, body: UpdateSection, user: dict = Depends(current_user)):
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM detail_pages WHERE product_id=? ORDER BY updated_at DESC LIMIT 1",
            (product_id,),
        ).fetchone()
        if not existing:
            raise HTTPException(404, "상세페이지 없음")
        updates = []
        params = []
        if body.sections is not None:
            updates.append("sections=?")
            params.append(json.dumps(body.sections, ensure_ascii=False))
        if body.html_content is not None:
            updates.append("html_content=?")
            params.append(body.html_content)
        if not updates:
            raise HTTPException(400, "변경 내용 없음")
        updates.append("updated_at=CURRENT_TIMESTAMP")
        params.append(existing["id"])
        conn.execute(
            f"UPDATE detail_pages SET {', '.join(updates)} WHERE id=?",
            params,
        )
    return {"ok": True}
