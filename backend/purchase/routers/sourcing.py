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
            f"""SELECT id, keyword_id, asin, title, amazon_url, image_url,
                       price_usd, price_krw,
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
    # ★2026-08-12 차단 — 새 임포트 경로(~/asin_import.py)로 대체됨.
    #   이 경로는 sourcing_candidates 로 직행해 M6 선별·M7 건수 가드를 우회한다.
    #   되살리려면 PA_ALLOW_LEGACY_IMPORT=1 로 실행할 것.
    import os as _os
    if _os.environ.get("PA_ALLOW_LEGACY_IMPORT") != "1":
        raise HTTPException(
            410,
            "구형 시트 임포트는 중단됐습니다. asin_import.py 를 사용하세요 "
            "(CSV·XLSX·구글시트 모두 지원). "
            "python3 ~/asin_import.py <입력> --batch <배치명> --channel <채널> --apply"
        )
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


# ── 시트 큐 자동 파이프라인 (대량 import) ────────────────────

class QueueAddBody(BaseModel):
    sheets: list[dict]  # [{"url": "...", "label": "..."}, ...]


@router.post("/queue")
def queue_add(body: QueueAddBody, user: dict = Depends(current_user)):
    """시트 URL 리스트를 큐에 추가. 워커가 순차 자동 처리."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    added = 0
    with get_db() as conn:
        for sheet in body.sheets:
            url = (sheet.get("url") or "").strip()
            if not url:
                continue
            label = (sheet.get("label") or "").strip()[:100]
            conn.execute(
                """INSERT INTO sheet_queue (sheet_url, sheet_label, queued_at)
                   VALUES (?, ?, ?)""",
                (url, label or None, now),
            )
            added += 1
    return {"added": added}


@router.get("/queue")
def queue_list(user: dict = Depends(current_user)):
    """큐 전체 조회 — 진행 중 + 완료 + 에러 모두."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM sheet_queue ORDER BY queued_at DESC LIMIT 100"""
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.delete("/queue/{queue_id}")
def queue_cancel(queue_id: int, user: dict = Depends(current_user)):
    """queued 상태만 취소 가능. 진행 중인 시트는 취소 불가."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM sheet_queue WHERE id=?", (queue_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "큐 항목 없음")
        if row["status"] != "queued":
            raise HTTPException(400, f"queued 상태만 취소 가능 (현재: {row['status']})")
        conn.execute(
            "UPDATE sheet_queue SET status='cancelled', finished_at=datetime('now') WHERE id=?",
            (queue_id,),
        )
    return {"ok": True}


# ── 키워드 → SP-API → 부모그룹 → 멀티옵션 등록 (#2 신규 흐름) ──
class KeywordGroupingReq(BaseModel):
    keyword: str                                   # 한글 입력 시 자동 영문 번역
    max_groups: int = 5                            # 처리할 부모 그룹 상한 (1~20)
    channels: Optional[list[str]] = None           # 기본 ["coupang"]
    dry_run: bool = True                           # True=빌드만, False=라이브 등록
    requested: bool = False                        # 라이브 시 False=임시저장(권장,삭제가능)/True=자동승인요청


@router.post("/keyword-grouping")
async def keyword_grouping(body: KeywordGroupingReq, user: dict = Depends(current_user)):
    """키워드 1개 → SP-API search → 부모 그룹 수집 → 멀티옵션 쿠팡 등록.

    파이프라인: 한글이면 영문 번역 → search_catalog_items → 부모 dedupe →
    각 부모마다 resync_group_from_spapi + register_new_group_listing(dry_run).
    """
    from fastapi.concurrency import run_in_threadpool
    from backend.purchase.services.keyword_to_groups import (
        translate_keyword_to_english, process_keyword,
    )

    if not body.keyword or not body.keyword.strip():
        raise HTTPException(400, "keyword 가 비어있음")
    if not (1 <= body.max_groups <= 20):
        raise HTTPException(400, "max_groups 는 1~20")

    kw_en = await translate_keyword_to_english(body.keyword.strip())
    result = await run_in_threadpool(
        process_keyword, kw_en,
        max_groups=body.max_groups,
        channels=body.channels or ["coupang"],
        dry_run=body.dry_run,
        requested=body.requested,
    )
    result["original_keyword"] = body.keyword
    result["search_keyword"] = kw_en
    return result


# ── 키워드 큐 (양산 자동화) ─────────────────────────────
class KeywordQueueAddReq(BaseModel):
    keywords: list[str]                            # 한글/영문 다수 키워드
    max_groups: int = 5
    channels: Optional[list[str]] = None           # 기본 ["coupang"]
    requested: bool = False                        # 기본 임시저장
    dry_run: bool = False                          # 큐는 보통 라이브용 (드라이런은 endpoint 직접)


@router.post("/keyword-queue/add")
def keyword_queue_add(body: KeywordQueueAddReq, user: dict = Depends(current_user)):
    """키워드를 큐에 적재 (daemon 이 60초 폴링으로 순차 처리)."""
    from backend.purchase.services.keyword_queue_worker import _ensure_table
    _ensure_table()
    kws = [k.strip() for k in (body.keywords or []) if k and k.strip()]
    if not kws:
        raise HTTPException(400, "keywords 가 비어있음")
    if not (1 <= body.max_groups <= 20):
        raise HTTPException(400, "max_groups 는 1~20")
    channels_csv = ",".join(body.channels or ["coupang"])
    inserted = []
    with get_db() as conn:
        for kw in kws:
            cur = conn.execute(
                """INSERT INTO keyword_queue (keyword, max_groups, channels, requested, dry_run)
                   VALUES (?, ?, ?, ?, ?)""",
                (kw, body.max_groups, channels_csv, int(body.requested), int(body.dry_run)),
            )
            inserted.append(cur.lastrowid)
    return {"enqueued": len(inserted), "ids": inserted}


@router.get("/keyword-queue")
def keyword_queue_list(
    user: dict = Depends(current_user),
    status: Optional[str] = None,
    limit: int = 50,
):
    """큐 목록 조회. status 미지정 시 최근 N개 전체."""
    from backend.purchase.services.keyword_queue_worker import _ensure_table
    _ensure_table()
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM keyword_queue WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, max(1, min(limit, 500))),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM keyword_queue ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
    return {"items": [dict(r) for r in rows]}


# ── Retrofit: 기존 master listing 에 옵션 묶기 (PUT 교체) ──
class RetrofitExtendReq(BaseModel):
    parent_asin: str
    seller_product_id: str
    dry_run: bool = True
    requested: bool = False                        # 기본 임시저장 (안전)


@router.post("/retrofit-extend")
async def retrofit_extend(body: RetrofitExtendReq, user: dict = Depends(current_user)):
    """기존 라이브 쿠팡 master 에 그룹의 풀 items[] 를 신규 파이프라인으로 빌드해 PUT.

    resync(정합성+누락 형제 ingest) + AI detailing(title_ko 채움) + build_coupang_payload(신규)
    → 기존 master GET → items+sellerProductName 교체 → PUT. 라이브 PUT 은 재심사 트리거.
    """
    from fastapi.concurrency import run_in_threadpool
    from backend.purchase.services.group_lister import retrofit_extend_with_rebuild

    if not body.parent_asin or not body.seller_product_id:
        raise HTTPException(400, "parent_asin / seller_product_id 필수")
    result = await run_in_threadpool(
        retrofit_extend_with_rebuild,
        body.parent_asin.strip(),
        body.seller_product_id.strip(),
        dry_run=body.dry_run,
        requested=body.requested,
    )
    return result


@router.delete("/keyword-queue/{queue_id}")
def keyword_queue_cancel(queue_id: int, user: dict = Depends(current_user)):
    """queued 상태만 취소 가능. processing/done/error 는 취소 불가."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM keyword_queue WHERE id=?", (queue_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "큐 항목 없음")
        if row["status"] != "queued":
            raise HTTPException(400, f"queued 상태만 취소 가능 (현재: {row['status']})")
        conn.execute(
            "UPDATE keyword_queue SET status='cancelled', finished_at=datetime('now') WHERE id=?",
            (queue_id,),
        )
    return {"ok": True}


# ── 그룹 등록 큐 (부모 ASIN 단위, group_registration_worker 가 직렬 처리) ──
class GroupQueueAddBody(BaseModel):
    parent_asins: Optional[list[str]] = None       # 직접 ASIN list
    sheet_url: Optional[str] = None                # 또는 시트 URL + tab → ASIN dedup
    sheet_tab: Optional[str] = None
    requested: bool = False                         # 임시저장 기본
    channels: str = "coupang"                       # "coupang" 또는 "coupang,smartstore"


@router.post("/group-queue/add")
async def group_queue_add(body: GroupQueueAddBody, user: dict = Depends(current_user)):
    """그룹 등록 큐에 부모 ASIN 추가. 워커가 60초 폴링으로 순차 처리.

    입력 모드:
      - parent_asins: ASIN list 직접 등록
      - sheet_url+sheet_tab: 시트 ASIN → SP-API 부모 dedupe (느림, 비동기 권장)
    """
    from fastapi.concurrency import run_in_threadpool
    parents: list[str] = []
    sheet_id: Optional[str] = None
    if body.parent_asins:
        for a in body.parent_asins:
            a = (a or "").strip().upper()
            if len(a) == 10 and a.isalnum() and a not in parents:
                parents.append(a)
    elif body.sheet_url and body.sheet_tab:
        from backend.purchase.services.sheet_importer import (
            extract_sheet_id, discover_tabs, fetch_tab_csv,
        )
        from backend.purchase.scripts.sheet_to_groups import get_parent_asin
        sheet_id = extract_sheet_id(body.sheet_url)
        if not sheet_id:
            raise HTTPException(400, "sheet_id 추출 실패")
        tabs, _ = discover_tabs(sheet_id)
        target = next((t for t in tabs if t.get("name") == body.sheet_tab), None)
        if not target:
            raise HTTPException(404, f"탭 '{body.sheet_tab}' 없음")
        rows = await run_in_threadpool(fetch_tab_csv, sheet_id, int(target["gid"]))
        asins = []
        for r in rows:
            a = (r.get("ASIN") or "").strip().upper()
            if len(a) == 10 and a.isalnum():
                asins.append(a)
        # SP-API 부모 dedupe (직렬, rate limit 0.5s)
        seen = set()
        for asin in asins:
            p = await run_in_threadpool(get_parent_asin, asin)
            if p and p not in seen:
                seen.add(p)
                parents.append(p)
            import asyncio
            await asyncio.sleep(0.5)
    else:
        raise HTTPException(400, "parent_asins 또는 sheet_url+sheet_tab 필요")

    if not parents:
        return {"added": 0, "parents": []}

    added = 0
    with get_db() as conn:
        for pa in parents:
            conn.execute(
                """INSERT INTO group_registration_queue
                   (parent_asin, sheet_id, sheet_tab, requested, channels)
                   VALUES (?, ?, ?, ?, ?)""",
                (pa, sheet_id, body.sheet_tab, 1 if body.requested else 0, body.channels),
            )
            added += 1
    return {"added": added, "parents": parents}


@router.get("/group-queue")
def group_queue_list(user: dict = Depends(current_user)):
    """그룹 등록 큐 전체 조회 (queued/pre_scanning/registering/done/error/skipped)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM group_registration_queue ORDER BY queued_at DESC LIMIT 200"
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.delete("/group-queue/{queue_id}")
def group_queue_cancel(queue_id: int, user: dict = Depends(current_user)):
    """queued 상태만 취소 가능. processing/done/error 는 불가."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM group_registration_queue WHERE id=?", (queue_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "큐 항목 없음")
        if row["status"] != "queued":
            raise HTTPException(400, f"queued 상태만 취소 가능 (현재: {row['status']})")
        conn.execute(
            "UPDATE group_registration_queue SET status='cancelled', "
            "finished_at=datetime('now') WHERE id=?",
            (queue_id,),
        )
    return {"ok": True}
