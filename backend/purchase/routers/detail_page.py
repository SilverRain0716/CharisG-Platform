"""PA Detail Page — AI 번역 + SEO + 상세페이지 HTML 생성."""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services.ai_processor import (
    process_product,
    create_batch_job,
    get_batch_job,
    get_running_job,
    run_batch_background,
    run_two_stage_batch,
)

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


# ── 백그라운드 배치 생성 ──────────────────────────

class BatchBody(BaseModel):
    product_ids: list[int] | None = None
    all_unprocessed: bool = False
    all_products: bool = False
    platform: str = "smartstore"
    two_stage: bool = True   # default: HTML 먼저 → AI 자동 (사용자 한 번 클릭)


@router.post("/batch")
async def batch_generate(body: BatchBody, user: dict = Depends(current_user)):
    # 이미 실행 중인 job이 있으면 거부
    running = get_running_job()
    if running:
        raise HTTPException(409, f"이미 실행 중인 배치 job 있음: {running['id']}")

    if body.all_products:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id FROM products WHERE business_model='purchase' ORDER BY id"
            ).fetchall()
        product_ids = [r["id"] for r in rows]
    elif body.all_unprocessed:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id FROM products WHERE ai_processed_at IS NULL ORDER BY id"
            ).fetchall()
        product_ids = [r["id"] for r in rows]
    elif body.product_ids:
        product_ids = body.product_ids
    else:
        raise HTTPException(400, "product_ids, all_unprocessed, 또는 all_products 필요")

    if not product_ids:
        raise HTTPException(400, "처리 대상 상품 없음")

    job_id = create_batch_job(product_ids, body.platform)
    if body.two_stage:
        asyncio.create_task(run_two_stage_batch(job_id, product_ids, body.platform))
    else:
        asyncio.create_task(run_batch_background(job_id, product_ids, body.platform))
    return {"job_id": job_id, "total": len(product_ids), "two_stage": body.two_stage}


@router.get("/batch/{job_id}")
def batch_status(job_id: str, user: dict = Depends(current_user)):
    job = get_batch_job(job_id)
    if not job:
        raise HTTPException(404, "배치 job 없음")
    pct = round(((job["processed"] + job["errors"]) / job["total"]) * 100, 1) if job["total"] else 0
    return {**job, "pct": pct}


@router.get("/batch")
def batch_current(user: dict = Depends(current_user)):
    """현재 실행 중인 job 조회. 없으면 null."""
    job = get_running_job()
    if not job:
        return {"job": None}
    pct = round(((job["processed"] + job["errors"]) / job["total"]) * 100, 1) if job["total"] else 0
    return {"job": {**job, "pct": pct}}


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
