"""PA Coupang — 쿠팡 리스팅 조회 + WING 업로드."""
import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.services.image_downloader import mark_images_for_deletion, download_product_images
from backend.purchase.services.coupang_service import get_orders, sync_orders
from backend.purchase.services.coupang_lister import list_product
from backend.purchase.services.coupang_meta import get_category_meta
from backend.purchase.services.coupang_attributes import _validate_ai_value, extract_mandatory_strict
from backend.purchase.services import coupang_approval

# KST (UTC+9) — 쿠팡 API는 KST 시각 기준.
KST = timezone(timedelta(hours=9))

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
def list_listings(
    user: dict = Depends(current_user),
    status: str | None = None,
    limit: int = 10000,   # 사실상 paginate 비활성 — frontend client filter 호환
    offset: int = 0,
):
    where = ["l.channel='coupang'"]
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
            "SELECT COUNT(*) c FROM listings_pa WHERE channel='coupang' AND status='pending'"
        ).fetchone()["c"]
        total_listed = conn.execute(
            "SELECT COUNT(*) c FROM listings_pa WHERE channel='coupang' AND status='listed'"
        ).fetchone()["c"]
    return {
        "items": [dict(r) for r in rows],
        "totals": {"pending": total_pending, "listed": total_listed},
        "approval_pending": coupang_approval.count_pending_approval(),
    }


@router.post("/upload/{product_id}")
def upload(product_id: int, user: dict = Depends(current_user)):
    """단일 상품 업로드 — group master 면 multi-option, 아니면 단품."""
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
                parent_asin=vg["parent_asin"], channels=["coupang"], dry_run=False,
            )
            return {"ok": True, "mode": "group", "result": res}

    # 단품 등록
    result = list_product(product_id)
    if result.get("ok"):
        mark_images_for_deletion(product_id)
    return result


# ── 속성 보정 (MANDATORY 누락 excluded 복구) ────────────────────

_PAT_MANDATORY_ATTR = re.compile(r"MANDATORY\s*'([^']+)'")


@router.get("/excluded")
def list_excluded(user: dict = Depends(current_user)):
    """MANDATORY 속성 부족으로 excluded 된 상품 목록 + 누락 속성별 집계.

    coupang_attributes_json (v24+ 채널 전용 컬럼, dict) 에 저장된 값을 함께 반환해
    UI 에서 "이미 수동 저장했는지" 표시할 수 있도록.
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT l.product_id, l.error_message, l.coupang_category_code,
                      p.asin, p.title_ko, p.title_en,
                      p.coupang_attributes_json, p.category_path, p.images_json
               FROM listings_pa l JOIN products p ON p.id = l.product_id
               WHERE l.channel='coupang' AND l.status='excluded'
                 AND l.error_message LIKE 'MANDATORY%'
               ORDER BY l.product_id DESC"""
        ).fetchall()
    items = []
    by_attr: dict[str, int] = {}
    for r in rows:
        d = dict(r)
        m = _PAT_MANDATORY_ATTR.search(d.get("error_message") or "")
        attr_name = m.group(1) if m else ""
        d["missing_attr"] = attr_name
        saved = {}
        try:
            inf = json.loads(d.get("coupang_attributes_json") or "{}")
            if isinstance(inf, dict):
                saved = inf
        except (json.JSONDecodeError, TypeError):
            saved = {}
        d["saved_attrs"] = saved
        d.pop("coupang_attributes_json", None)
        # images_json 은 UI 에서 첫 이미지 썸네일에 쓰도록 원본 그대로 전달
        items.append(d)
        if attr_name:
            by_attr[attr_name] = by_attr.get(attr_name, 0) + 1
    return {"items": items, "by_attr": by_attr, "total": len(items)}


@router.post("/attributes/bulk")
def save_attributes_bulk(body: dict, user: dict = Depends(current_user)):
    """속성값 수동 저장. products.coupang_attributes_json (dict) 머지.

    body: {"items": [{"product_id": int, "attrs": {name: value or null}}]}
    값이 null/빈 문자열이면 해당 key 삭제.
    """
    items = body.get("items") or []
    if not isinstance(items, list):
        raise HTTPException(400, "items 배열이 필요합니다")

    saved = 0
    for it in items:
        pid = it.get("product_id")
        new_attrs = it.get("attrs") or {}
        if not isinstance(pid, int) or not isinstance(new_attrs, dict):
            continue
        with get_db() as conn:
            row = conn.execute(
                "SELECT coupang_attributes_json FROM products WHERE id=?", (pid,)
            ).fetchone()
            if not row:
                continue
            coupang_attrs: dict = {}
            raw = row["coupang_attributes_json"]
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        coupang_attrs = parsed
                except (json.JSONDecodeError, TypeError):
                    coupang_attrs = {}
            for k, v in new_attrs.items():
                key = str(k).strip()
                if not key:
                    continue
                if v in (None, ""):
                    coupang_attrs.pop(key, None)
                elif isinstance(v, str) and v.strip():
                    coupang_attrs[key] = v.strip()
            conn.execute(
                "UPDATE products SET coupang_attributes_json=? WHERE id=?",
                (json.dumps(coupang_attrs, ensure_ascii=False), pid),
            )
        saved += 1
    return {"saved": saved}


@router.post("/restore-pending")
def restore_pending(body: dict, user: dict = Depends(current_user)):
    """선택한 상품의 listings_pa.status 를 excluded → pending 으로 복구.

    업로드 재시도 대기 풀로 넣는 단순 동작. 이미지/속성 상태 검증은 하지 않음.
    """
    pids_raw = body.get("product_ids") or []
    pids = [p for p in pids_raw if isinstance(p, int)]
    if not pids:
        return {"restored": 0}
    with get_db() as conn:
        placeholders = ",".join("?" * len(pids))
        cur = conn.execute(
            f"""UPDATE listings_pa SET status='pending', error_message=NULL,
                last_synced_at=NULL
                WHERE channel='coupang' AND status='excluded'
                  AND product_id IN ({placeholders})""",
            pids,
        )
        restored = cur.rowcount
    return {"restored": restored}


@router.post("/reextract-strict")
async def reextract_strict(body: dict, user: dict = Depends(current_user)):
    """SP-API + 강화 AI 프롬프트로 MANDATORY 속성 재추출 (배치 job).

    body:
      - product_ids: [int] — 대상 상품 (우선순위 1)
      - attr_name: str — 특정 누락 속성 하나만 대상으로 자동 수집 (우선순위 2)
      - limit: int — attr_name 사용 시 처리 건수 제한

    결과는 products.coupang_attributes_json (dict) 에 머지.
    복구(pending 전환)는 하지 않음 — 운영자가 UI 에서 확인 후 restore-pending 호출.
    """
    pids_in = body.get("product_ids") or []
    attr_name = (body.get("attr_name") or "").strip()
    limit = int(body.get("limit") or 0)

    pids: list[int] = [p for p in pids_in if isinstance(p, int)]
    if not pids and attr_name:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT product_id FROM listings_pa
                   WHERE channel='coupang' AND status='excluded'
                     AND error_message LIKE ?
                   ORDER BY product_id DESC""",
                (f"MANDATORY '{attr_name}'%",),
            ).fetchall()
        pids = [r["product_id"] for r in rows]
        if limit > 0:
            pids = pids[:limit]

    if not pids:
        raise HTTPException(400, "대상 product_ids 또는 attr_name 지정 필요")

    # 중복 job 방지
    with get_db() as conn:
        running = conn.execute(
            "SELECT * FROM batch_jobs WHERE job_type='coupang_reextract' AND status IN ('pending','running') LIMIT 1"
        ).fetchone()
    if running:
        raise HTTPException(409, f"이미 실행 중인 재추출 job: {running['id']}")

    job_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            """INSERT INTO batch_jobs (id, job_type, status, total, created_at)
               VALUES (?, 'coupang_reextract', 'pending', ?, ?)""",
            (job_id, len(pids), _now_iso()),
        )
    asyncio.create_task(_run_reextract_bg(job_id, pids))
    return {"job_id": job_id, "total": len(pids)}


@router.get("/reextract-strict/{job_id}")
def reextract_status(job_id: str, user: dict = Depends(current_user)):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "job 없음")
    job = dict(row)
    pct = round(((job["processed"] + job["errors"]) / job["total"]) * 100, 1) if job["total"] else 0
    return {**job, "pct": pct}


async def _run_reextract_bg(job_id: str, product_ids: list[int]):
    """SP-API 쿼터 보수적으로 사용하기 위해 concurrency 2로 고정."""
    sem = asyncio.Semaphore(int(os.environ.get("REEXTRACT_CONCURRENCY", "2")))
    counter_lock = asyncio.Lock()
    processed = 0
    errors = 0

    with get_db() as conn:
        conn.execute(
            "UPDATE batch_jobs SET status='running', started_at=? WHERE id=?",
            (_now_iso(), job_id),
        )

    async def _one(pid: int):
        nonlocal processed, errors
        async with sem:
            try:
                res = await asyncio.to_thread(extract_mandatory_strict, pid)
                async with counter_lock:
                    processed += 1 if res.get("extracted") else 0
                    errors += 0 if res.get("extracted") else 1
                if not res.get("extracted"):
                    logger.info(f"[coupang-reextract] pid={pid} 추출 0개")
            except Exception as e:
                async with counter_lock:
                    errors += 1
                logger.warning(f"[coupang-reextract] pid={pid} 예외: {e}")
            async with counter_lock:
                # 매 5건마다 UPDATE (sqlite 충돌 방지)
                done = processed + errors
                if done % 5 == 0 or done == len(product_ids):
                    with get_db() as conn:
                        conn.execute(
                            "UPDATE batch_jobs SET processed=?, errors=?, current_product_id=? WHERE id=?",
                            (processed, errors, pid, job_id),
                        )

    await asyncio.gather(*[_one(p) for p in product_ids])

    with get_db() as conn:
        conn.execute(
            """UPDATE batch_jobs SET status='done', processed=?, errors=?, finished_at=?,
               current_product_id=NULL, phase_message=? WHERE id=?""",
            (processed, errors, _now_iso(),
             f"완료 — 채움 {processed}, 실패 {errors}/{len(product_ids)}", job_id),
        )
    logger.info(
        f"[coupang-reextract] 완료 — 채움 {processed}, 실패 {errors}/{len(product_ids)}"
    )


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
    import time as _time
    concurrency = int(os.environ.get("COUPANG_UPLOAD_CONCURRENCY", "4"))
    sem = asyncio.Semaphore(max(1, concurrency))
    counter_lock = asyncio.Lock()

    started_at = _time.time()
    processed = 0
    errors = 0
    skipped = 0

    with get_db() as conn:
        conn.execute(
            "UPDATE batch_jobs SET status='running', started_at=? WHERE id=?",
            (_now_iso(), job_id),
        )

    # ── Phase 0: 이미지 누락 자동 재다운로드 ──
    # image_cache에 public_url 행이 없는 상품은 쿠팡 pull이 불가능해 페이로드 검증에서 실패.
    # 업로드 직전 단일 기회로 아마존 원본에서 재다운로드 시도. 실패는 그대로 Phase 2에 넘겨 excluded 처리.
    with get_db() as conn:
        placeholders = ",".join("?" * len(product_ids))
        with_img_rows = conn.execute(
            f"""SELECT DISTINCT product_id FROM image_cache
                WHERE product_id IN ({placeholders}) AND public_url IS NOT NULL""",
            product_ids,
        ).fetchall()
    pids_with_images = {r["product_id"] for r in with_img_rows}
    missing_pids = [pid for pid in product_ids if pid not in pids_with_images]

    with get_db() as conn:
        conn.execute(
            "UPDATE batch_jobs SET phase_message=? WHERE id=?",
            (f"Phase 0 이미지 사전점검 — 누락 {len(missing_pids)}건 재다운로드", job_id),
        )

    redl_ok = 0
    redl_fail = 0

    async def _redownload(pid: int):
        nonlocal redl_ok, redl_fail
        async with sem:
            with get_db() as conn:
                p = conn.execute("SELECT images_json FROM products WHERE id=?", (pid,)).fetchone()
            if not p or not p["images_json"]:
                async with counter_lock:
                    redl_fail += 1
                return
            try:
                r = await download_product_images(pid, p["images_json"])
                async with counter_lock:
                    if r.get("downloaded", 0) > 0:
                        redl_ok += 1
                    else:
                        redl_fail += 1
            except Exception as e:
                async with counter_lock:
                    redl_fail += 1
                logger.warning(f"[coupang-upload-all] pid={pid} 재다운로드 예외: {e}")

    if missing_pids:
        await asyncio.gather(*[_redownload(p) for p in missing_pids])
        logger.info(
            f"[coupang-upload-all] Phase 0 완료 — 재다운로드 성공 {redl_ok}/{len(missing_pids)} "
            f"(실패 {redl_fail})"
        )
    else:
        logger.info(f"[coupang-upload-all] Phase 0 생략 — 전원({len(product_ids)}) 이미지 보유")

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
                # 매 5건마다 UPDATE (sqlite 충돌 방지)
                done = processed + errors
                if done % 5 == 0 or done == len(product_ids):
                    with get_db() as conn:
                        conn.execute(
                            "UPDATE batch_jobs SET processed=?, errors=?, current_product_id=? WHERE id=?",
                            (processed, errors, pid, job_id),
                        )

    await asyncio.gather(*[_register(pid) for pid in product_ids], return_exceptions=False)

    duration_sec = _time.time() - started_at
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

    # Discord 알림
    try:
        from backend.purchase.services.notifier import notify_upload_complete
        notify_upload_complete(
            channel="coupang", success=processed, errors=errors,
            total=len(product_ids), duration_sec=duration_sec,
        )
    except Exception as e:
        logger.warning(f"[coupang-upload-all] Discord 알림 실패 (무시): {e}")


@router.get("/orders")
def fetch_orders(start: str, end: str, user: dict = Depends(current_user)):
    return {"orders": get_orders(start, end) or []}


def _format_kst_date(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%Y-%m-%d")


@router.post("/orders/sync")
def sync_orders_now(days: int = 2, user: dict = Depends(current_user)):
    """수동 동기화 트리거. 최근 N일(기본 2) 범위의 쿠팡 ACCEPT 주문을 upsert.

    쿠팡 ordersheet API는 yyyy-MM-dd 단위 — 시간 정밀도 없음.
    """
    if days < 1 or days > 7:
        raise HTTPException(400, "days는 1~7 범위")
    now = datetime.now(tz=KST)
    start = _format_kst_date(now - timedelta(days=days - 1))
    end = _format_kst_date(now)
    result = sync_orders(start, end)
    # batch_jobs에 1-row 스냅샷 기록 (대시보드 last_coupang_sync용).
    job_id = uuid.uuid4().hex[:12]
    msg = (
        f"수동 동기화 [{start} ~ {end}] — 조회 {result.get('fetched', 0)}, "
        f"신규 {result.get('inserted', 0)}, 중복 {result.get('duplicated', 0)}, "
        f"매핑실패 {result.get('unmapped', 0)}, 에러 {result.get('errors', 0)}"
    )
    with get_db() as conn:
        conn.execute(
            """INSERT INTO batch_jobs (id, job_type, status, total, processed, errors,
                phase_message, created_at, started_at, finished_at)
               VALUES (?, 'coupang_order_sync', 'done', ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                result.get("fetched", 0),
                result.get("inserted", 0),
                result.get("errors", 0),
                msg,
                _now_iso(),
                _now_iso(),
                _now_iso(),
            ),
        )
    return {"job_id": job_id, "range": {"start": start, "end": end}, **result}


@router.get("/orders/sync/last")
def last_order_sync(user: dict = Depends(current_user)):
    """마지막 쿠팡 주문 동기화 상태 (대시보드/UI에서 last_coupang_sync 표시용)."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT id, status, total, processed, errors, phase_message, finished_at, started_at
               FROM batch_jobs
               WHERE job_type='coupang_order_sync'
               ORDER BY COALESCE(finished_at, started_at, created_at) DESC
               LIMIT 1"""
        ).fetchone()
    return {"job": dict(row) if row else None}


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
