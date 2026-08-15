"""coupang_new.py — 신계정(A01731680, 카리스글로벌) 신규 등록 API.

기존 /api/pa/coupang/* 는 pa-api 프로세스 default(COUPANG_ACTIVE=old)라 구계정으로 감.
이 라우터는 신계정으로 신규 등록하는 별도 경로. `with S.coupang_account("new"):`
컨텍스트를 명시해서 자격증명·vendorId 를 신계정으로 라우팅.

엔드포인트:
  POST /api/pa/coupang-new/upload-single   body: { asin }                      → { job_id }
  POST /api/pa/coupang-new/upload-group    body: { parent_asin, old_cpid? }   → { job_id }
  GET  /api/pa/coupang-new/job/{job_id}    → batch_jobs 행 + pct
  GET  /api/pa/coupang-new/regroup/status  → regroup.db + purchase.db 카운트 (조회만)

배경 유틸(EC2 정본): urgent_upload.py / urgent_group.py / re_group_existing.py
"""
import logging
import os
import sqlite3
import threading
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.purchase.database import get_db
from backend.purchase.auth import current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pa/coupang-new", tags=["pa-coupang-new"])

REGROUP_DB = "/home/ubuntu/CharisG-Platform/charisg-platform/regroup.db"


# ────────── Request bodies ──────────

class UploadSingleBody(BaseModel):
    asin: str


class UploadGroupBody(BaseModel):
    parent_asin: str
    old_cpid: Optional[str] = None


# ────────── Background workers ──────────

def _job_phase(job_id: str, msg: str, processed: int = 0):
    """batch_jobs.status='running' + phase_message 갱신."""
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET status='running', "
                "started_at=COALESCE(started_at, datetime('now')), "
                "phase_message=?, processed=? WHERE id=?",
                (msg, processed, job_id),
            )
    except Exception:
        logger.exception("phase update 실패 (job=%s)", job_id)


def _job_done(job_id: str, ok: bool, summary: str, error: Optional[str] = None,
              processed: int = 1):
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET status=?, processed=?, phase_message=?, "
                "error_message=?, finished_at=datetime('now') WHERE id=?",
                ("done" if ok else "error", processed, summary, error, job_id),
            )
    except Exception:
        logger.exception("done update 실패 (job=%s)", job_id)


def _run_new_single_bg(job_id: str, asin: str):
    """단품 등록 백그라운드 — urgent_upload.py 파이프라인을 API로."""
    from backend.purchase.services import coupang_service as S
    try:
        from backend.purchase.scripts.group_registration_worker import _register_single_fallback
    except ImportError as e:
        _job_done(job_id, False, f"import 실패: {e}", str(e)[:400], 0)
        return

    _job_phase(job_id, f"SP-API → AI → 등록 (신계정, ASIN={asin})")

    try:
        with S.coupang_account("new"):
            res, err = _register_single_fallback(asin, requested=True)
    except Exception as e:
        logger.exception("upload-single 백그라운드 예외 (asin=%s)", asin)
        _job_done(job_id, False, "예외 발생", str(e)[:400], 0)
        return

    if err:
        _job_done(job_id, False, f"등록 실패: {str(err)[:80]}", str(err)[:400], 0)
        return

    ok = bool(res.get("ok")) if isinstance(res, dict) else False
    err_msg = None
    summary = "성공"
    if not ok and isinstance(res, dict):
        err_msg = str(res.get("error") or "unknown")[:400]
        summary = f"실패: {err_msg[:80]}"

    # 최신 신계정 리스팅 조회 (성공 시)
    if ok:
        try:
            with get_db() as conn:
                row = conn.execute(
                    """SELECT l.channel_product_id, l.status, l.sale_krw, p.title_ko
                       FROM listings_pa l JOIN products p ON p.id=l.product_id
                       WHERE p.asin=? AND l.channel='coupang' AND l.coupang_account='new'
                       ORDER BY l.id DESC LIMIT 1""",
                    (asin,),
                ).fetchone()
            if row:
                summary = (
                    f"cpid={row['channel_product_id']} 상태={row['status']} "
                    f"가격={row['sale_krw']}원 · {(row['title_ko'] or '')[:30]}"
                )
        except Exception:
            logger.exception("등록 결과 조회 실패")

    _job_done(job_id, ok, summary, err_msg)


def _run_new_group_bg(job_id: str, parent_asin: str, old_cpid: Optional[str]):
    """그룹 등록 백그라운드 — urgent_group.py 파이프라인을 API로.
    old_cpid 있으면 stop_sales 후 register_new_group_listing."""
    from backend.purchase.services import coupang_service as S
    try:
        from backend.purchase.services.group_lister import register_new_group_listing
    except ImportError as e:
        _job_done(job_id, False, f"import 실패: {e}", str(e)[:400], 0)
        return

    try:
        with S.coupang_account("new"):
            if old_cpid:
                _job_phase(job_id, f"기존 단품 판매중지 (cpid={old_cpid})")
                try:
                    S.stop_sales(str(old_cpid))
                except Exception as e:
                    logger.warning("stop_sales 실패(계속): %s", e)

            _job_phase(job_id, f"그룹 등록 (parent={parent_asin})")
            res = register_new_group_listing(
                parent_asin, channels=["coupang"], dry_run=False, requested=True
            )
    except Exception as e:
        logger.exception("upload-group 백그라운드 예외 (parent=%s)", parent_asin)
        _job_done(job_id, False, "예외 발생", str(e)[:400], 0)
        return

    # 결과 파싱: channels.coupang[] 배열 스캔
    coupang_results = ((res or {}).get("channels") or {}).get("coupang") or []
    if not isinstance(coupang_results, list):
        coupang_results = []
    successful = [r for r in coupang_results
                  if isinstance(r, dict) and r.get("status") == "registered"
                  and r.get("channel_product_id")]

    if successful:
        first = successful[0]
        summary = (
            f"cpid={first.get('channel_product_id')} "
            f"옵션={first.get('options_persisted', '?')}개 "
            f"(split={len(successful)}개)"
        )
        _job_done(job_id, True, summary)
    else:
        err_msg = "등록 결과 없음"
        if coupang_results and isinstance(coupang_results[0], dict):
            err_msg = str(coupang_results[0].get("error") or coupang_results[0].get("status") or err_msg)[:400]
        _job_done(job_id, False, f"실패: {err_msg[:80]}", err_msg)


# ────────── Endpoints ──────────

@router.post("/upload-single")
def upload_single(body: UploadSingleBody, user: dict = Depends(current_user)):
    asin = (body.asin or "").strip().upper()
    if not asin or not asin.startswith("B0") or len(asin) < 8:
        raise HTTPException(400, "ASIN 형식 오류 (예: B0DX964WJR)")

    job_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            """INSERT INTO batch_jobs (id, job_type, status, total, processed,
                phase_message, created_at)
               VALUES (?, 'coupang_new_single', 'pending', 1, 0, ?, datetime('now'))""",
            (job_id, f"대기 (ASIN={asin})"),
        )
    threading.Thread(
        target=_run_new_single_bg, args=(job_id, asin), daemon=True
    ).start()
    return {"job_id": job_id, "total": 1, "asin": asin}


@router.post("/upload-group")
def upload_group(body: UploadGroupBody, user: dict = Depends(current_user)):
    parent = (body.parent_asin or "").strip().upper()
    if not parent or not parent.startswith("B0") or len(parent) < 8:
        raise HTTPException(400, "PARENT ASIN 형식 오류")
    old_cpid = (body.old_cpid or "").strip() or None

    job_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            """INSERT INTO batch_jobs (id, job_type, status, total, processed,
                phase_message, created_at)
               VALUES (?, 'coupang_new_group', 'pending', 1, 0, ?, datetime('now'))""",
            (job_id, f"대기 (parent={parent})"),
        )
    threading.Thread(
        target=_run_new_group_bg, args=(job_id, parent, old_cpid), daemon=True
    ).start()
    return {"job_id": job_id, "total": 1, "parent_asin": parent, "old_cpid": old_cpid}


@router.get("/job/{job_id}")
def get_job(job_id: str, user: dict = Depends(current_user)):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "job 없음")
    job = dict(row)
    total = job.get("total") or 1
    processed = job.get("processed") or 0
    pct = round((processed / total) * 100, 1) if total else 0
    return {**job, "pct": pct}


@router.get("/regroup/status")
def regroup_status(user: dict = Depends(current_user)):
    """re_group_existing.py::run_status 로직을 REST 로 이식.
    regroup.db(regroup_scan/regroup_exclude) + purchase.db(listings_pa/group_registration_queue) 조회만.
    scan/enqueue 실행은 여전히 SSH CLI 사용."""
    result = {
        "regroup_scan": {"parent": 0, "child": 0, "single": 0, "fail": 0},
        "unscanned": 0,
        "exclude_count": 0,
        "regroup_queued": 0,
        "selling_children": 0,
        "regroup_db_exists": False,
    }

    if not os.path.exists(REGROUP_DB):
        return result

    result["regroup_db_exists"] = True
    scanned: set[int] = set()
    try:
        rc = sqlite3.connect(REGROUP_DB, timeout=15)
        rc.row_factory = sqlite3.Row
        try:
            sc = {r["kind"]: r["c"] for r in rc.execute(
                "SELECT kind, COUNT(*) c FROM regroup_scan GROUP BY kind"
            ).fetchall()}
            for k in ("parent", "child", "single", "fail"):
                if k in sc:
                    result["regroup_scan"][k] = sc[k]
            result["exclude_count"] = rc.execute(
                "SELECT COUNT(*) c FROM regroup_exclude"
            ).fetchone()["c"]
            result["selling_children"] = rc.execute(
                "SELECT COUNT(*) c FROM regroup_scan WHERE kind='child' AND selling=1"
            ).fetchone()["c"]
            scanned = {r["product_id"] for r in rc.execute(
                "SELECT product_id FROM regroup_scan"
            ).fetchall()}
        finally:
            rc.close()

        with get_db() as pc:
            allp = {r["pid"] for r in pc.execute(
                "SELECT p.id pid FROM listings_pa l JOIN products p ON p.id=l.product_id "
                "WHERE l.channel='coupang' AND p.asin IS NOT NULL GROUP BY p.id"
            ).fetchall()}
            result["unscanned"] = len(allp - scanned)
            result["regroup_queued"] = pc.execute(
                "SELECT COUNT(*) c FROM group_registration_queue "
                "WHERE sheet_id='regroup:existing'"
            ).fetchone()["c"]
    except Exception as e:
        logger.exception("regroup_status 오류")
        result["error"] = str(e)[:200]

    return result
