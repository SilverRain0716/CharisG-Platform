"""PA Sourcing — 시트 import, 후보 리스트, 선택 삭제, 상품관리 이관."""
import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services.sheet_importer import import_from_sheet_url
from backend.purchase.services.sourcing_promote import (
    create_promote_job,
    get_promote_job,
    get_running_promote_job,
    run_promote_background,
)

router = APIRouter(prefix="/api/pa/sourcing", tags=["pa-sourcing"])


@router.get("")
def list_candidates(
    user: dict = Depends(current_user),
    status: Optional[str] = None,
    shipping: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
):
    where = []
    params: list = []
    if status:
        where.append("sourcing_status=?")
        params.append(status)
    if shipping:
        where.append("shipping_status=?")
        params.append(shipping)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT id, keyword_id, asin, title, amazon_url, image_url, price_usd,
                       rating, review_count, monthly_sales, category, notes,
                       in_stock, cj_filter_pass, shipping_status, sourcing_status,
                       collected_at
                FROM sourcing_candidates {where_sql}
                ORDER BY collected_at DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) c FROM sourcing_candidates {where_sql}", tuple(params),
        ).fetchone()["c"]
    return {"items": [dict(r) for r in rows], "total": total}


class ImportSheetBody(BaseModel):
    sheet_url: str


@router.post("/import-sheet")
def import_sheet(body: ImportSheetBody, user: dict = Depends(current_user)):
    try:
        result = import_from_sheet_url(body.sheet_url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if result.get("error") == "PERMISSION_DENIED":
        raise HTTPException(403, result.get("message") or "시트가 비공개 상태입니다")
    return result


class BulkDeleteBody(BaseModel):
    ids: list[int]


@router.post("/bulk-delete")
def bulk_delete(body: BulkDeleteBody, user: dict = Depends(current_user)):
    if not body.ids:
        return {"deleted": 0}
    placeholders = ",".join("?" * len(body.ids))
    with get_db() as conn:
        cur = conn.execute(
            f"DELETE FROM sourcing_candidates WHERE id IN ({placeholders})",
            tuple(body.ids),
        )
        deleted = cur.rowcount
    return {"deleted": deleted}


# ── 상품관리로 전체 이관 (백그라운드 job) ──

def _pct(job: dict) -> float:
    total = job.get("total") or 0
    if not total:
        return 0.0
    return round(((job.get("processed") or 0) + (job.get("errors") or 0)) / total * 100, 1)


@router.post("/promote-all")
async def promote_all_start(user: dict = Depends(current_user)):
    """이관 job 시작. job_id 즉시 반환, 실제 처리는 백그라운드에서 진행."""
    running = get_running_promote_job()
    if running:
        raise HTTPException(409, f"이미 실행 중인 이관 job 있음: {running['id']}")

    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM sourcing_candidates"
        ).fetchone()["c"]
    if not total:
        raise HTTPException(400, "이관할 후보 없음")

    job_id = create_promote_job(total)
    asyncio.create_task(run_promote_background(job_id))
    return {"job_id": job_id, "total": total}


@router.get("/promote-all")
def promote_all_current(user: dict = Depends(current_user)):
    """현재 실행 중인 이관 job 조회. 없으면 null."""
    job = get_running_promote_job()
    if not job:
        return {"job": None}
    return {"job": {**job, "pct": _pct(job)}}


@router.get("/promote-all/{job_id}")
def promote_all_status(job_id: str, user: dict = Depends(current_user)):
    job = get_promote_job(job_id)
    if not job:
        raise HTTPException(404, "이관 job 없음")
    return {**job, "pct": _pct(job)}
