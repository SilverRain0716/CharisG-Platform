"""PA Coupang — 쿠팡 리스팅 조회 + WING 업로드."""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services.image_downloader import mark_images_for_deletion
from backend.purchase.services.coupang_service import register_product, get_orders

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pa/coupang", tags=["pa-coupang"])

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/listings")
def list_listings(user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT l.*, p.title_ko, p.title_en, p.asin
               FROM listings_pa l JOIN products p ON l.product_id = p.id
               WHERE l.channel = 'coupang'
               ORDER BY l.id DESC""",
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/upload/{product_id}")
def upload(product_id: int, user: dict = Depends(current_user)):
    with get_db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        raise HTTPException(404, "상품 없음")

    payload = {
        "displayCategoryCode": p["category_path"] or "",
        "sellerProductName": p["title_ko"] or p["title_en"],
        "salePrice": int(p["sale_price_krw"] or 0),
        "originalPrice": int(p["sale_price_krw"] or 0),
        "items": [{"sellerProductItemName": "기본"}],
    }
    result = register_product(payload)
    if not result:
        return {"ok": False, "error": "쿠팡 API 호출 실패"}

    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO listings_pa
               (product_id, channel, channel_product_id, status, last_synced_at)
               VALUES (?, 'coupang', ?, 'listed', CURRENT_TIMESTAMP)""",
            (product_id, str(result.get("data", "") if isinstance(result, dict) else "")),
        )
    mark_images_for_deletion(product_id)
    return {"ok": True, "result": result}


def _upload_single_coupang(pid: int) -> dict:
    with get_db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        return {"ok": False, "error": f"상품 {pid} 없음"}

    payload = {
        "displayCategoryCode": p["category_path"] or "",
        "sellerProductName": p["title_ko"] or p["title_en"],
        "salePrice": int(p["sale_price_krw"] or 0),
        "originalPrice": int(p["sale_price_krw"] or 0),
        "items": [{"sellerProductItemName": "기본"}],
    }
    result = register_product(payload)
    if not result:
        return {"ok": False, "error": "쿠팡 API 호출 실패"}

    with get_db() as conn:
        conn.execute(
            """UPDATE listings_pa SET channel_product_id=?, status='listed',
               last_synced_at=CURRENT_TIMESTAMP WHERE product_id=? AND channel='coupang'""",
            (str(result.get("data", "") if isinstance(result, dict) else ""), pid),
        )
    mark_images_for_deletion(pid)
    return {"ok": True, "result": result}


@router.post("/upload-all")
async def upload_all(user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT l.product_id FROM listings_pa l
               JOIN products p ON l.product_id = p.id
               WHERE l.channel='coupang' AND l.status='pending'
               ORDER BY l.product_id"""
        ).fetchall()
    if not rows:
        raise HTTPException(400, "업로드 대상 없음 (pending 상태 리스팅 필요)")

    with get_db() as conn:
        running = conn.execute(
            "SELECT * FROM batch_jobs WHERE job_type='coupang_upload' AND status IN ('pending','running') LIMIT 1"
        ).fetchone()
    if running:
        raise HTTPException(409, f"이미 실행 중인 업로드 job 있음: {running['id']}")

    product_ids = [r["product_id"] for r in rows]
    job_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            """INSERT INTO batch_jobs (id, job_type, status, total, created_at)
               VALUES (?, 'coupang_upload', 'pending', ?, ?)""",
            (job_id, len(product_ids), _now_iso()),
        )
    asyncio.create_task(_run_coupang_upload_bg(job_id, product_ids))
    return {"job_id": job_id, "total": len(product_ids)}


@router.get("/upload-all/{job_id}")
def upload_status(job_id: str, user: dict = Depends(current_user)):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "job 없음")
    job = dict(row)
    pct = round(((job["processed"] + job["errors"]) / job["total"]) * 100, 1) if job["total"] else 0
    return {**job, "pct": pct}


@router.get("/upload-job")
def upload_current(user: dict = Depends(current_user)):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM batch_jobs WHERE job_type='coupang_upload' AND status IN ('pending','running') ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return {"job": None}
    job = dict(row)
    pct = round(((job["processed"] + job["errors"]) / job["total"]) * 100, 1) if job["total"] else 0
    return {"job": {**job, "pct": pct}}


async def _run_coupang_upload_bg(job_id: str, product_ids: list[int]):
    processed = 0
    errors = 0

    with get_db() as conn:
        conn.execute("UPDATE batch_jobs SET status='running', started_at=? WHERE id=?", (_now_iso(), job_id))

    for pid in product_ids:
        try:
            res = _upload_single_coupang(pid)
            if not res.get("ok"):
                raise ValueError(res.get("error", "업로드 실패"))
            processed += 1
        except Exception as e:
            errors += 1
            logger.warning(f"[coupang-upload-all] product {pid} 실패: {e}")

        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET processed=?, errors=?, current_product_id=? WHERE id=?",
                (processed, errors, pid, job_id),
            )
        await asyncio.sleep(0)

    with get_db() as conn:
        conn.execute(
            """UPDATE batch_jobs SET status='done', processed=?, errors=?, finished_at=?,
               current_product_id=NULL WHERE id=?""",
            (processed, errors, _now_iso(), job_id),
        )
    logger.info(f"[coupang-upload-all] 완료 — 성공 {processed}, 실패 {errors}/{len(product_ids)}")


@router.get("/orders")
def fetch_orders(start: str, end: str, user: dict = Depends(current_user)):
    return {"orders": get_orders(start, end) or []}
