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
def list_listings(
    user: dict = Depends(current_user),
    status: str | None = None,
    limit: int = 10000,   # 사실상 paginate 비활성 — frontend client filter 호환
    offset: int = 0,
):
    where = ["l.channel='smartstore'"]
    params: list = []
    if status:
        where.append("l.status=?"); params.append(status)
    where_sql = " AND ".join(where)
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT l.*, p.title_ko, p.title_en, p.asin
               FROM listings_pa l JOIN products p ON l.product_id = p.id
               WHERE {where_sql}
               ORDER BY l.id DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        total_pending = conn.execute(
            "SELECT COUNT(*) c FROM listings_pa WHERE channel='smartstore' AND status='pending'"
        ).fetchone()["c"]
        total_listed = conn.execute(
            "SELECT COUNT(*) c FROM listings_pa WHERE channel='smartstore' AND status='listed'"
        ).fetchone()["c"]
        total_excluded = conn.execute(
            "SELECT COUNT(*) c FROM listings_pa WHERE channel='smartstore' AND status='excluded'"
        ).fetchone()["c"]
    return {
        "items": [dict(r) for r in rows],
        "totals": {"pending": total_pending, "listed": total_listed, "excluded": total_excluded},
    }


@router.post("/upload/{product_id}")
def upload(product_id: int, user: dict = Depends(current_user)):
    # 그룹 인식: product 가 variation_group 일원 (master) 이면 multi-option 등록
    with get_db() as conn:
        prod = conn.execute(
            "SELECT asin, parent_asin, is_group_master FROM products WHERE id=?", (product_id,)
        ).fetchone()
    if prod and prod["parent_asin"] and prod["is_group_master"]:
        with get_db() as conn:
            vg = conn.execute(
                "SELECT parent_asin FROM variation_groups WHERE parent_asin=? OR master_asin=? LIMIT 1",
                (prod["parent_asin"], prod["asin"]),
            ).fetchone()
        if vg:
            from backend.purchase.services.group_lister import register_new_group_listing
            res = register_new_group_listing(
                parent_asin=vg["parent_asin"], channels=["smartstore"], dry_run=False,
            )
            return {"ok": True, "mode": "group", "result": res}

    # 단품 등록
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


@router.post("/resume-pending")
async def resume_pending(user: dict = Depends(current_user)):
    """smartstore 잔여 pending 모두 swap(자동) + register.

    sheet_queue 취소/한도 초과 fail 정리 후 wing 정리 끝나면 호출.
    한도 회전 (영구삭제 swap) 자동 trigger 포함.
    """
    running = _get_running_upload("smartstore")
    if running:
        raise HTTPException(409, f"이미 실행 중인 업로드 job 있음: {running['id']}")

    from backend.purchase.services.smartstore_resume import resume_smartstore_pending
    # 즉시 반환 — swap+register 는 background
    asyncio.create_task(resume_smartstore_pending())
    return {"started": True}


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
    """카테고리별 conveyor 파이프라인: 이미지 → 추론 → 등록 streaming.

    각 카테고리 task 가 chunks(10) 단위로 (이미지 → 추론 → 등록) 흐름을 직렬 처리하고,
    카테고리 task 들은 sem 으로 자연 직렬화되어 conveyor 형성.

    이득: 첫 등록까지 ~수십 초 (현재 phase-major 는 batch 시간 의 절반 이상 소요).
    """
    import os
    import time as _time
    from itertools import groupby
    import json as _json
    from backend.purchase.routers.smartstore_attributes import (
        _get_attrs_with_values, _infer_batch_same_category, _map_ai_to_attrs,
    )

    concurrency = int(os.environ.get("SMARTSTORE_UPLOAD_CONCURRENCY", "1"))
    sem = asyncio.Semaphore(max(1, concurrency))
    gemini_sem = asyncio.Semaphore(1)  # Gemini rate limit (분당 RPM 회피)
    counter_lock = asyncio.Lock()

    started_at = _time.time()
    counters = {
        "processed": 0, "errors": 0, "skipped": 0,
        "inferred": 0, "cached": 0, "skipped_no_cat": 0, "skipped_no_attrs": 0,
        "img_ok": 0, "img_fail": 0,
    }
    total = len(product_ids)

    with get_db() as conn:
        conn.execute(
            """UPDATE batch_jobs SET status='running', started_at=?,
               phase='phase_2', phase_message=? WHERE id=?""",
            (_now_iso(),
             f"카테고리별 streaming 시작 — 대상 {total}건",
             job_id),
        )

    # ── 카테고리 정렬 + naver_attributes_json 캐시 여부 함께 로드 ──
    if not product_ids:
        with get_db() as conn:
            conn.execute(
                """UPDATE batch_jobs SET status='done', phase='done',
                   finished_at=?, phase_message=? WHERE id=?""",
                (_now_iso(), "대상 0건", job_id),
            )
        return

    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, category_path, title_ko, title_en, naver_attributes_json
               FROM products WHERE id IN ({})""".format(",".join("?" * len(product_ids))),
            product_ids,
        ).fetchall()
    products_data = [dict(r) for r in rows]
    products_data.sort(key=lambda p: p["category_path"] or "")

    # 매 N 건 마다 batch_jobs UPDATE (DB 부담 완화)
    async def _flush_progress(current_pid: int | None = None):
        async with counter_lock:
            done = counters["processed"] + counters["errors"] + counters["skipped"]
            msg = (
                f"streaming — 등록 {counters['processed']} / 실패 {counters['errors']} "
                f"/ 제외 {counters['skipped']} / 이미지OK {counters['img_ok']}/{total} "
                f"/ 추론 {counters['inferred']} (캐시 {counters['cached']})"
            )
            with get_db() as conn:
                conn.execute(
                    """UPDATE batch_jobs SET processed=?, errors=?, phase_message=?,
                       current_product_id=? WHERE id=?""",
                    (counters["processed"], counters["errors"], msg, current_pid, job_id),
                )

    async def _preupload_one(pid: int) -> tuple[int, list[str]]:
        async with sem:
            try:
                urls = await asyncio.to_thread(preupload_images, pid)
            except Exception as e:
                logger.warning(f"[{channel}] product {pid} 이미지 업로드 실패: {e}")
                urls = []
        async with counter_lock:
            if urls:
                counters["img_ok"] += 1
            else:
                counters["img_fail"] += 1
        return pid, urls

    async def _register_one(pid: int, urls: list[str]) -> None:
        async with sem:
            if not urls:
                with get_db() as conn:
                    conn.execute(
                        """UPDATE listings_pa SET status='excluded', error_message=?,
                           last_synced_at=CURRENT_TIMESTAMP
                           WHERE product_id=? AND channel='smartstore'""",
                        ("이미지 업로드 실패 (네이버 429 또는 로컬 파일 누락)", pid),
                    )
                async with counter_lock:
                    counters["errors"] += 1
                logger.warning(f"[{channel}-upload-all] product {pid} 이미지 없음 → 스킵")
                return
            try:
                res = await asyncio.to_thread(list_product, pid, image_urls=urls)
                if res.get("skip"):
                    async with counter_lock:
                        counters["skipped"] += 1
                    logger.info(f"[{channel}-upload-all] product {pid} 제외: {res.get('error')}")
                elif not res.get("ok"):
                    async with counter_lock:
                        counters["errors"] += 1
                    logger.warning(f"[{channel}-upload-all] product {pid} 실패: {res.get('error')}")
                else:
                    mark_images_for_deletion(pid)
                    async with counter_lock:
                        counters["processed"] += 1
            except Exception as e:
                async with counter_lock:
                    counters["errors"] += 1
                logger.warning(f"[{channel}-upload-all] product {pid} 예외: {e}")

    async def _process_category(cat_id: str, products: list[dict]):
        """한 카테고리의 모든 product 를 chunks(10) 단위로 streaming 처리."""
        # 1. 속성 메타 조회 (캐시 hit) — 카테고리 유효 시
        attrs_with_values: list[dict] = []
        if cat_id and cat_id.isdigit():
            try:
                attrs_with_values = await asyncio.to_thread(_get_attrs_with_values, cat_id)
            except Exception as e:
                logger.warning(f"[{channel}-upload-all] cat={cat_id} 속성 메타 조회 실패: {e}")
                attrs_with_values = []
            if not attrs_with_values:
                async with counter_lock:
                    counters["skipped_no_attrs"] += len(products)
        else:
            async with counter_lock:
                counters["skipped_no_cat"] += len(products)

        # 2. chunks(10) streaming
        for i in range(0, len(products), 10):
            chunk = products[i:i + 10]
            chunk_pids = [p["id"] for p in chunk]

            # 2a. 이미지 (chunk 단위, sem 으로 직렬)
            pid_url_pairs = await asyncio.gather(
                *[_preupload_one(pid) for pid in chunk_pids],
                return_exceptions=False,
            )
            chunk_image_map = dict(pid_url_pairs)

            # 2b. 추론 (need_infer 만, gemini_sem 으로 카테고리 동시성 1 제한)
            need_infer = [
                p for p in chunk
                if p.get("naver_attributes_json") is None
                and chunk_image_map.get(p["id"])  # 이미지 실패한 건 skip
            ]
            already_cached = [
                p for p in chunk
                if p.get("naver_attributes_json") is not None
                and chunk_image_map.get(p["id"])
            ]
            async with counter_lock:
                counters["cached"] += len(already_cached)

            if attrs_with_values and need_infer:
                async with gemini_sem:
                    try:
                        ai_results = await _infer_batch_same_category(need_infer, attrs_with_values)
                    except Exception as e:
                        logger.warning(f"[{channel}-upload-all] cat={cat_id} 배치 추론 실패: {e}")
                        ai_results = {}
                    for p in need_infer:
                        pid = p["id"]
                        mapped = _map_ai_to_attrs(ai_results.get(pid, []), attrs_with_values)
                        # 매핑 성공/실패 모두 마킹 (다음 실행 시 재추론 방지)
                        payload_json = _json.dumps(mapped) if mapped else "[]"
                        with get_db() as conn:
                            conn.execute(
                                "UPDATE products SET naver_attributes_json=? WHERE id=?",
                                (payload_json, pid),
                            )
                        if mapped:
                            async with counter_lock:
                                counters["inferred"] += 1
                    await asyncio.sleep(1)  # Gemini rate limit

            # 2c. 등록 (chunk 단위, sem 으로 직렬 — 다른 카테고리 task 와 sem 경쟁)
            await asyncio.gather(
                *[_register_one(pid, chunk_image_map.get(pid, [])) for pid in chunk_pids],
                return_exceptions=False,
            )

            # 2d. 진척 flush
            await _flush_progress(current_pid=chunk_pids[-1] if chunk_pids else None)

    # ── 카테고리별 task 동시 시작 (sem 으로 자연 직렬화 → conveyor) ──
    cat_tasks = []
    for cat_id, grp in groupby(products_data, key=lambda p: p["category_path"] or ""):
        cat_tasks.append(_process_category(cat_id, list(grp)))

    # return_exceptions=True — 한 카테고리 task 가 raise 해도 다른 카테고리 진행
    cat_results = await asyncio.gather(*cat_tasks, return_exceptions=True)
    for r in cat_results:
        if isinstance(r, Exception):
            logger.error(f"[{channel}-upload-all] 카테고리 task 예외: {r}")

    # 마지막 진척 flush
    await _flush_progress()

    duration_sec = _time.time() - started_at
    summary = (
        f"완료 — 등록 {counters['processed']}, 제외 {counters['skipped']}, "
        f"실패 {counters['errors']}, 추론 {counters['inferred']} "
        f"(캐시 {counters['cached']}, 카테고리X {counters['skipped_no_cat']}, "
        f"속성X {counters['skipped_no_attrs']})"
    )
    with get_db() as conn:
        conn.execute(
            """UPDATE batch_jobs SET status='done', phase='done',
               processed=?, errors=?,
               finished_at=?, current_product_id=NULL, phase_message=? WHERE id=?""",
            (counters["processed"], counters["errors"], _now_iso(), summary, job_id),
        )
    logger.info(f"[{channel}-upload-all] {summary} (총 {duration_sec:.1f}초)")

    # Discord 알림
    try:
        from backend.purchase.services.notifier import notify_upload_complete
        notify_upload_complete(
            channel=channel, success=counters["processed"], errors=counters["errors"],
            total=total, duration_sec=duration_sec,
        )
    except Exception as e:
        logger.warning(f"[{channel}-upload-all] Discord 알림 실패 (무시): {e}")


@router.get("/preview/{product_id}")
def preview(product_id: int, user: dict = Depends(current_user)):
    payload = build_payload(product_id)
    if not payload:
        raise HTTPException(404, "상품 없음")
    return payload
