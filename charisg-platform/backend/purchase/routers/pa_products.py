"""PA Products — 활성 상품 목록 + 상세 + 상태 변경 + 채널 준비 job."""
import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services.channel_listing_service import send_to_channels
from backend.purchase.services import channel_prepare

router = APIRouter(prefix="/api/pa/products", tags=["pa-products"])


def _pct(job: dict) -> float:
    total = job.get("total") or 0
    if not total:
        return 0.0
    return round(((job.get("processed") or 0) + (job.get("errors") or 0)) / total * 100, 1)


@router.get("")
def list_products(
    user: dict = Depends(current_user),
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    unchanneled_only: bool = False,
):
    """상품 목록. unchanneled_only=True 면 listings_pa 행이 하나도 없는 상품만 (= 아직 채널로 보낸 적 없음).

    status='archived' 상품은 기본적으로 숨김 (업로드 실패 정리분).
    명시적으로 status='archived' 를 파라미터로 주면 조회 가능.
    """
    where = ["p.business_model='purchase'"]
    params: list = []
    if status:
        where.append("p.status=?")
        params.append(status)
    else:
        # 기본: archived 제외 (업로드 실패 정리분 숨김)
        where.append("(p.status IS NULL OR p.status != 'archived')")
    if unchanneled_only:
        where.append("NOT EXISTS (SELECT 1 FROM listings_pa l WHERE l.product_id = p.id)")
    where_sql = " AND ".join(where)
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT p.id, p.asin, p.title_ko, p.title_en, p.sale_price_krw, p.cost_usd, p.margin_pct,
                       p.category_path, p.status, p.bsr, p.ai_processed_at, p.seo_title, p.created_at
                FROM products p WHERE {where_sql}
                ORDER BY p.id DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) c FROM products p WHERE {where_sql}", tuple(params),
        ).fetchone()["c"]
        unprocessed_count = conn.execute(
            f"SELECT COUNT(*) c FROM products p WHERE {where_sql} AND p.ai_processed_at IS NULL",
            tuple(params),
        ).fetchone()["c"]
        processed_count = conn.execute(
            f"SELECT COUNT(*) c FROM products p WHERE {where_sql} AND p.ai_processed_at IS NOT NULL",
            tuple(params),
        ).fetchone()["c"]
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "unprocessed_count": unprocessed_count,
        "processed_count": processed_count,
        "naver_category_pending": channel_prepare.count_naver_pending(),
        "coupang_category_pending": channel_prepare.count_coupang_pending(),
    }


@router.get("/{pid}")
def get_product(pid: int, user: dict = Depends(current_user)):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "상품 없음")
        listings = conn.execute(
            "SELECT * FROM listings_pa WHERE product_id=?", (pid,),
        ).fetchall()
        margin = conn.execute(
            """SELECT * FROM margin_calcs WHERE sourcing_id=
               (SELECT sourcing_id FROM products WHERE id=?) ORDER BY id DESC LIMIT 1""",
            (pid,),
        ).fetchone()
    return {
        "product": dict(row),
        "listings": [dict(l) for l in listings],
        "margin": dict(margin) if margin else None,
    }


class SendToChannelBody(BaseModel):
    channels: list[str] = ["smartstore", "coupang"]


@router.post("/{pid}/send-to-channel")
def send_to_channel(pid: int, body: SendToChannelBody, user: dict = Depends(current_user)):
    try:
        result = send_to_channels(pid, body.channels)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return result


@router.post("/bulk-send-to-channel")
def bulk_send_to_channel(body: SendToChannelBody, user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id FROM products
               WHERE business_model='purchase' AND ai_processed_at IS NOT NULL
                 AND cost_usd IS NOT NULL
                 AND (status IS NULL OR status != 'archived')
               ORDER BY id"""
        ).fetchall()
    if not rows:
        raise HTTPException(400, "채널 전송 대상 없음 (AI 처리 완료 + cost_usd 필요)")

    results = []
    errors = []
    for r in rows:
        try:
            res = send_to_channels(r["id"], body.channels)
            results.append(res)
        except Exception as e:
            errors.append({"product_id": r["id"], "error": str(e)})

    return {"sent": len(results), "errors": len(errors), "error_details": errors}


class PriceBody(BaseModel):
    sale_price_krw: int


@router.patch("/{pid}/price")
def set_price(pid: int, body: PriceBody, user: dict = Depends(current_user)):
    if body.sale_price_krw < 0:
        raise HTTPException(400, "판매가는 0 이상이어야 함")
    with get_db() as conn:
        row = conn.execute("SELECT cost_usd FROM products WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "상품 없음")
        cost_usd = row["cost_usd"] or 0
        from backend.purchase.services.exchange_rate_service import get_current_rate
        fx = get_current_rate()
        cost_krw = cost_usd * fx
        margin_pct = ((body.sale_price_krw - cost_krw) / body.sale_price_krw * 100) if body.sale_price_krw > 0 else 0
        conn.execute(
            """UPDATE products SET sale_price_krw=?, margin_pct=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (body.sale_price_krw, round(margin_pct, 1), pid),
        )
        conn.execute(
            """UPDATE listings_pa SET sale_krw=?, net_margin_krw=?, updated_at=CURRENT_TIMESTAMP
               WHERE product_id=?""",
            (body.sale_price_krw, int(body.sale_price_krw * margin_pct / 100), pid),
        )
    return {"ok": True, "sale_price_krw": body.sale_price_krw, "margin_pct": round(margin_pct, 1)}


class StatusBody(BaseModel):
    status: str


@router.patch("/{pid}/status")
def set_status(pid: int, body: StatusBody, user: dict = Depends(current_user)):
    if body.status not in {"draft", "ready", "listed", "active", "paused", "removed"}:
        raise HTTPException(400, "invalid status")
    with get_db() as conn:
        conn.execute(
            "UPDATE products SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (body.status, pid),
        )
    return {"ok": True}


def _cascade_delete_products(conn, product_ids: list[int]) -> dict:
    """products + listings_pa + detail_pages + image_cache cascade 삭제.
    호출자가 connection 관리 (with get_db() as conn).
    """
    if not product_ids:
        return {"products": 0, "listings_pa": 0, "detail_pages": 0, "image_cache": 0}
    placeholders = ",".join("?" * len(product_ids))
    counts = {}
    for table in ("image_cache", "detail_pages", "listings_pa"):
        cur = conn.execute(
            f"DELETE FROM {table} WHERE product_id IN ({placeholders})",
            product_ids,
        )
        counts[table] = cur.rowcount
    cur = conn.execute(
        f"DELETE FROM products WHERE id IN ({placeholders})",
        product_ids,
    )
    counts["products"] = cur.rowcount
    return counts


class BulkDeleteBody(BaseModel):
    ids: list[int] | None = None
    channel: str | None = None
    status: str | None = None  # e.g. 'excluded'


@router.post("/bulk-delete")
def bulk_delete(body: BulkDeleteBody, user: dict = Depends(current_user)):
    """상품 일괄 삭제 (cascade). 두 가지 모드:
    - ids 지정: 정확히 해당 product_id 들 삭제
    - channel + status 지정: 그 채널/상태의 listings_pa가 가리키는 product 전체 삭제
    """
    if body.ids:
        ids = [int(i) for i in body.ids]
    elif body.channel and body.status:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT DISTINCT product_id FROM listings_pa
                   WHERE channel=? AND status=?""",
                (body.channel, body.status),
            ).fetchall()
        ids = [r["product_id"] for r in rows]
    else:
        raise HTTPException(400, "ids 또는 channel+status 필수")

    if not ids:
        return {"deleted": {"products": 0}, "ids": []}

    with get_db() as conn:
        counts = _cascade_delete_products(conn, ids)

    return {"deleted": counts, "ids": ids}


# ── 채널 준비 (카테고리 매핑 백그라운드 job) ─────────────────

@router.post("/prepare-naver-category")
async def prepare_naver_category_start(user: dict = Depends(current_user)):
    """products.category_path 의 텍스트 값을 네이버 leaf ID 로 매핑."""
    running = channel_prepare.get_running_job(channel_prepare.JOB_TYPE_NAVER)
    if running:
        raise HTTPException(409, f"이미 실행 중: {running['id']}")
    total = channel_prepare.count_naver_pending()
    if not total:
        raise HTTPException(400, "매핑 대상 없음")
    job_id = channel_prepare.create_job(channel_prepare.JOB_TYPE_NAVER, total)
    asyncio.create_task(channel_prepare.run_naver_category_background(job_id))
    return {"job_id": job_id, "total": total}


@router.get("/prepare-naver-category")
def prepare_naver_category_current(user: dict = Depends(current_user)):
    job = channel_prepare.get_running_job(channel_prepare.JOB_TYPE_NAVER)
    if not job:
        return {"job": None}
    return {"job": {**job, "pct": _pct(job)}}


@router.get("/prepare-naver-category/{job_id}")
def prepare_naver_category_status(job_id: str, user: dict = Depends(current_user)):
    job = channel_prepare.get_job(job_id, channel_prepare.JOB_TYPE_NAVER)
    if not job:
        raise HTTPException(404, "job 없음")
    return {**job, "pct": _pct(job)}


@router.post("/prepare-coupang-category")
async def prepare_coupang_category_start(user: dict = Depends(current_user)):
    """listings_pa 의 네이버 ID 를 쿠팡 카테고리 코드로 매핑."""
    running = channel_prepare.get_running_job(channel_prepare.JOB_TYPE_COUPANG)
    if running:
        raise HTTPException(409, f"이미 실행 중: {running['id']}")
    total = channel_prepare.count_coupang_pending()
    if not total:
        raise HTTPException(400, "매핑 대상 없음")
    job_id = channel_prepare.create_job(channel_prepare.JOB_TYPE_COUPANG, total)
    asyncio.create_task(channel_prepare.run_coupang_category_background(job_id))
    return {"job_id": job_id, "total": total}


@router.get("/prepare-coupang-category")
def prepare_coupang_category_current(user: dict = Depends(current_user)):
    job = channel_prepare.get_running_job(channel_prepare.JOB_TYPE_COUPANG)
    if not job:
        return {"job": None}
    return {"job": {**job, "pct": _pct(job)}}


@router.get("/prepare-coupang-category/{job_id}")
def prepare_coupang_category_status(job_id: str, user: dict = Depends(current_user)):
    job = channel_prepare.get_job(job_id, channel_prepare.JOB_TYPE_COUPANG)
    if not job:
        raise HTTPException(404, "job 없음")
    return {**job, "pct": _pct(job)}
