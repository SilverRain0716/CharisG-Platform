"""PA Smartstore — 네이버 스마트스토어 리스팅 조회 + 업로드."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services.image_downloader import mark_images_for_deletion
from backend.purchase.services.smartstore_lister import list_product, build_payload, preupload_images

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pa/smartstore", tags=["pa-smartstore"])

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/listings")
def list_listings(user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT l.*, p.title_ko, p.title_en, p.asin
               FROM listings_pa l JOIN products p ON l.product_id = p.id
               WHERE l.channel = 'smartstore'
               ORDER BY l.id DESC""",
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/upload/{product_id}")
def upload(product_id: int, user: dict = Depends(current_user)):
    result = list_product(product_id)
    if result.get("ok"):
        mark_images_for_deletion(product_id)
    return result


@router.post("/upload-all")
async def upload_all(user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT l.product_id FROM listings_pa l
               WHERE l.channel='smartstore' AND l.status='pending'
               ORDER BY l.product_id"""
        ).fetchall()
    if not rows:
        raise HTTPException(400, "업로드 대상 없음 (pending 상태 리스팅 필요)")

    running = _get_running_upload("smartstore")
    if running:
        raise HTTPException(409, f"이미 실행 중인 업로드 job 있음: {running['id']}")

    product_ids = [r["product_id"] for r in rows]
    job_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            """INSERT INTO batch_jobs (id, job_type, status, total, created_at)
               VALUES (?, 'smartstore_upload', 'pending', ?, ?)""",
            (job_id, len(product_ids), _now_iso()),
        )
    asyncio.create_task(_run_upload_background(job_id, product_ids, "smartstore"))
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
    job = _get_running_upload("smartstore")
    if not job:
        return {"job": None}
    pct = round(((job["processed"] + job["errors"]) / job["total"]) * 100, 1) if job["total"] else 0
    return {"job": {**job, "pct": pct}}


def _get_running_upload(channel: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM batch_jobs WHERE job_type=? AND status IN ('pending','running') ORDER BY created_at DESC LIMIT 1",
            (f"{channel}_upload",),
        ).fetchone()
    return dict(row) if row else None


async def _run_upload_background(job_id: str, product_ids: list[int], channel: str):
    """2단계 파이프라인: Phase 1(이미지 사전 업로드) → Phase 2(상품 등록).

    배치 이미지 업로드로 상품당 API 호출을 11→2로 줄여 ~6배 속도 향상.
    """
    import os
    concurrency = int(os.environ.get("SMARTSTORE_UPLOAD_CONCURRENCY", "4"))
    sem = asyncio.Semaphore(max(1, concurrency))
    counter_lock = asyncio.Lock()

    processed = 0
    errors = 0
    skipped = 0

    with get_db() as conn:
        conn.execute(
            "UPDATE batch_jobs SET status='running', started_at=? WHERE id=?",
            (_now_iso(), job_id),
        )

    # ── Phase 1: 이미지 사전 업로드 (배치 — 상품당 API 1회) ──
    image_map: dict[int, list[str]] = {}

    async def preupload(pid: int):
        async with sem:
            try:
                urls = await asyncio.to_thread(preupload_images, pid)
                image_map[pid] = urls
            except Exception as e:
                logger.warning(f"[{channel}] product {pid} 이미지 업로드 실패: {e}")
                image_map[pid] = []

    await asyncio.gather(*[preupload(pid) for pid in product_ids])
    img_ok = sum(1 for v in image_map.values() if v)
    logger.info(f"[{channel}-upload-all] Phase 1 완료 — 이미지 {img_ok}/{len(product_ids)}건 성공")

    # ── Phase 2: 상품 등록 (이미지 URL 재사용 — 상품당 API 1회) ──
    async def register(pid: int):
        nonlocal processed, errors, skipped
        urls = image_map.get(pid, [])

        async with sem:
            if not urls:
                async with counter_lock:
                    errors += 1
                logger.warning(f"[{channel}-upload-all] product {pid} 이미지 없음 → 스킵")
            else:
                try:
                    res = await asyncio.to_thread(list_product, pid, image_urls=urls)
                    if res.get("skip"):
                        async with counter_lock:
                            skipped += 1
                        logger.info(f"[{channel}-upload-all] product {pid} 제외: {res.get('error')}")
                    elif not res.get("ok"):
                        raise ValueError(res.get("error", "업로드 실패"))
                    else:
                        mark_images_for_deletion(pid)
                        async with counter_lock:
                            processed += 1
                except Exception as e:
                    async with counter_lock:
                        errors += 1
                    logger.warning(f"[{channel}-upload-all] product {pid} 실패: {e}")

            async with counter_lock:
                with get_db() as conn:
                    conn.execute(
                        "UPDATE batch_jobs SET processed=?, errors=?, current_product_id=? WHERE id=?",
                        (processed, errors, pid, job_id),
                    )

    await asyncio.gather(*[register(pid) for pid in product_ids], return_exceptions=False)

    with get_db() as conn:
        conn.execute(
            """UPDATE batch_jobs SET status='done', processed=?, errors=?, finished_at=?,
               current_product_id=NULL WHERE id=?""",
            (processed, errors, _now_iso(), job_id),
        )
    logger.info(f"[{channel}-upload-all] 완료 — 성공 {processed}, 제외 {skipped}, 실패 {errors}/{len(product_ids)}")


@router.get("/preview/{product_id}")
def preview(product_id: int, user: dict = Depends(current_user)):
    payload = build_payload(product_id)
    if not payload:
        raise HTTPException(404, "상품 없음")
    return payload
