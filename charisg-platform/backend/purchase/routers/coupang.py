"""PA Coupang — 쿠팡 리스팅 조회 + WING 업로드."""
import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services.image_downloader import mark_images_for_deletion
from backend.purchase.services.coupang_service import get_orders
from backend.purchase.services.coupang_lister import list_product
from backend.purchase.services.coupang_meta import get_category_meta
from backend.purchase.services import coupang_approval

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pa/coupang", tags=["pa-coupang"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_running_upload() -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM batch_jobs WHERE job_type='coupang_upload' AND status IN ('pending','running') ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


@router.get("/listings")
def list_listings(user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT l.*, p.title_ko, p.title_en, p.asin
               FROM listings_pa l JOIN products p ON l.product_id = p.id
               WHERE l.channel = 'coupang'
               ORDER BY l.id DESC""",
        ).fetchall()
    return {
        "items": [dict(r) for r in rows],
        "approval_pending": coupang_approval.count_pending_approval(),
    }


@router.post("/upload/{product_id}")
def upload(product_id: int, user: dict = Depends(current_user)):
    """단일 상품 업로드 — coupang_lister.list_product에 위임."""
    result = list_product(product_id)
    if result.get("ok"):
        mark_images_for_deletion(product_id)
    return result


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

    running = _get_running_upload()
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
    job = _get_running_upload()
    if not job:
        return {"job": None}
    pct = round(((job["processed"] + job["errors"]) / job["total"]) * 100, 1) if job["total"] else 0
    return {"job": {**job, "pct": pct}}


async def _run_coupang_upload_bg(job_id: str, product_ids: list[int]):
    """2단계 파이프라인: Phase 1.5(카테고리 메타 prefetch) → Phase 2(등록).

    스마트스토어의 이미지 사전업로드 URL을 재사용하므로 Phase 1(이미지)은 생략.
    쿠팡은 외부 https URL을 그대로 pull하므로 추가 업로드 불필요.
    """
    concurrency = int(os.environ.get("COUPANG_UPLOAD_CONCURRENCY", "4"))
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

    # ── Phase 1.5: 카테고리 메타 prefetch ──
    # unique displayCategoryCode 수집 → 메타 API 호출로 in-memory 캐시 warm-up.
    # build_payload 가 get_category_meta 동기 호출하므로 사전 채워두면 Phase 2 병목 해소.
    with get_db() as conn:
        placeholders = ",".join("?" * len(product_ids))
        cat_rows = conn.execute(
            f"""SELECT DISTINCT coupang_category_code FROM listings_pa
                WHERE channel='coupang' AND product_id IN ({placeholders})
                AND coupang_category_code IS NOT NULL""",
            product_ids,
        ).fetchall()
    unique_cats = [str(r["coupang_category_code"]) for r in cat_rows]

    with get_db() as conn:
        conn.execute(
            "UPDATE batch_jobs SET phase_message=? WHERE id=?",
            (f"Phase 1.5 카테고리 메타 prefetch — {len(unique_cats)}건", job_id),
        )

    prefetch_ok = 0
    prefetch_fail = 0

    async def _prefetch(cat: str):
        nonlocal prefetch_ok, prefetch_fail
        async with sem:
            try:
                meta = await asyncio.to_thread(get_category_meta, cat)
                async with counter_lock:
                    if meta:
                        prefetch_ok += 1
                    else:
                        prefetch_fail += 1
            except Exception as e:
                async with counter_lock:
                    prefetch_fail += 1
                logger.warning(f"[coupang-upload-all] cat={cat} 메타 prefetch 실패: {e}")

    if unique_cats:
        await asyncio.gather(*[_prefetch(c) for c in unique_cats])

    logger.info(
        f"[coupang-upload-all] Phase 1.5 완료 — 메타 캐시 {prefetch_ok}/{len(unique_cats)} "
        f"(실패 {prefetch_fail})"
    )

    # ── Phase 2: 등록 ──
    with get_db() as conn:
        conn.execute(
            "UPDATE batch_jobs SET phase_message=? WHERE id=?",
            (f"Phase 2 등록 진행 — 대상 {len(product_ids)}건", job_id),
        )

    async def _register(pid: int):
        nonlocal processed, errors, skipped
        async with sem:
            try:
                res = await asyncio.to_thread(list_product, pid)
                if res.get("skip"):
                    async with counter_lock:
                        skipped += 1
                    logger.info(f"[coupang-upload-all] product {pid} 제외: {res.get('error')}")
                elif not res.get("ok"):
                    raise ValueError(res.get("error", "업로드 실패"))
                else:
                    mark_images_for_deletion(pid)
                    async with counter_lock:
                        processed += 1
            except Exception as e:
                async with counter_lock:
                    errors += 1
                logger.warning(f"[coupang-upload-all] product {pid} 실패: {e}")

            async with counter_lock:
                with get_db() as conn:
                    conn.execute(
                        "UPDATE batch_jobs SET processed=?, errors=?, current_product_id=? WHERE id=?",
                        (processed, errors, pid, job_id),
                    )

    await asyncio.gather(*[_register(pid) for pid in product_ids], return_exceptions=False)

    with get_db() as conn:
        conn.execute(
            """UPDATE batch_jobs SET status='done', processed=?, errors=?, finished_at=?,
               current_product_id=NULL, phase_message=? WHERE id=?""",
            (processed, errors, _now_iso(),
             f"완료 — 성공 {processed}, 제외 {skipped}, 실패 {errors}", job_id),
        )
    logger.info(
        f"[coupang-upload-all] 완료 — 성공 {processed}, 제외 {skipped}, 실패 {errors}/{len(product_ids)}"
    )


@router.get("/orders")
def fetch_orders(start: str, end: str, user: dict = Depends(current_user)):
    return {"orders": get_orders(start, end) or []}


# ── 임시저장 → 승인 요청 (백그라운드 job) ──────────────────

@router.post("/request-approval-all")
async def request_approval_all(user: dict = Depends(current_user)):
    """listed 이지만 approval_requested_at NULL 인 쿠팡 리스팅에 대해
    PUT /seller-products/{id}/requests/approval 을 순회 호출."""
    running = coupang_approval.get_running_job()
    if running:
        raise HTTPException(409, f"이미 실행 중: {running['id']}")
    total = coupang_approval.count_pending_approval()
    if not total:
        raise HTTPException(400, "승인 요청 대상 없음")
    job_id = coupang_approval.create_job(total)
    asyncio.create_task(coupang_approval.run_approval_background(job_id))
    return {"job_id": job_id, "total": total}


@router.get("/request-approval-all")
def request_approval_current(user: dict = Depends(current_user)):
    job = coupang_approval.get_running_job()
    if not job:
        return {"job": None}
    pct = round(((job["processed"] + job["errors"]) / job["total"]) * 100, 1) if job["total"] else 0
    return {"job": {**job, "pct": pct}}


@router.get("/request-approval-all/{job_id}")
def request_approval_status(job_id: str, user: dict = Depends(current_user)):
    job = coupang_approval.get_job(job_id)
    if not job:
        raise HTTPException(404, "job 없음")
    pct = round(((job["processed"] + job["errors"]) / job["total"]) * 100, 1) if job["total"] else 0
    return {**job, "pct": pct}
