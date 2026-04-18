"""DS ASIN Pipeline — ASIN 매칭 + offer 등록 API."""
import threading
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from backend.dropshipping.auth import current_user
from backend.dropshipping.services import asin_matching_service, offer_registration_service

router = APIRouter(prefix="/api/ds/asin-pipeline", tags=["ds-asin-pipeline"])

# ── 매칭 파이프라인 상태 (in-memory) ──────────────────

_match_state: dict = {
    "running": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
    "started_at": None,
    "finished_at": None,
    "error": None,
    "result": None,
}
_match_lock = threading.Lock()

_offer_state: dict = {
    "running": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
    "started_at": None,
    "finished_at": None,
    "error": None,
    "result": None,
}
_offer_lock = threading.Lock()


def _match_progress_cb(phase: str, current: int, total: int, message: str):
    with _match_lock:
        _match_state["phase"] = phase
        _match_state["current"] = current
        _match_state["total"] = total
        _match_state["message"] = message


def _offer_progress_cb(phase: str, current: int, total: int, message: str):
    with _offer_lock:
        _offer_state["phase"] = phase
        _offer_state["current"] = current
        _offer_state["total"] = total
        _offer_state["message"] = message


# ── ASIN 매칭 ────────────────────────────────────────


@router.post("/match/single/{product_id}")
def match_single(product_id: int, user=Depends(current_user)):
    """단일 상품 ASIN 매칭."""
    try:
        best = asin_matching_service.find_best_match(product_id)
        candidates = asin_matching_service.get_candidates(product_id)
        return {
            "product_id": product_id,
            "best_match": best,
            "candidates": candidates,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


def _run_batch_match_bg(limit: int, min_sort_score: float):
    with _match_lock:
        _match_state.update({
            "running": True, "phase": "match", "current": 0, "total": 0,
            "message": "시작 중", "started_at": datetime.utcnow().isoformat(),
            "finished_at": None, "error": None, "result": None,
        })
    try:
        result = asin_matching_service.batch_match(
            limit=limit,
            min_sort_score=min_sort_score,
            progress_cb=_match_progress_cb,
        )
        with _match_lock:
            _match_state.update({
                "running": False, "phase": "done",
                "finished_at": datetime.utcnow().isoformat(),
                "result": {
                    "processed": result["processed"],
                    "matched": result["matched"],
                    "no_match": result["no_match"],
                    "failed": result["failed"],
                },
            })
    except Exception as e:
        with _match_lock:
            _match_state.update({
                "running": False, "phase": "error",
                "finished_at": datetime.utcnow().isoformat(),
                "error": str(e),
            })


@router.post("/match/batch")
def match_batch(
    background: BackgroundTasks,
    limit: int = 50,
    min_sort_score: float = 0.0,
    user=Depends(current_user),
):
    """일괄 ASIN 매칭 (백그라운드)."""
    with _match_lock:
        if _match_state["running"]:
            raise HTTPException(status_code=409, detail="매칭 작업 진행 중")
    background.add_task(_run_batch_match_bg, limit, min_sort_score)
    return {"status": "started", "limit": limit}


@router.get("/match/progress")
def match_progress(user=Depends(current_user)):
    """매칭 진행 상태 조회."""
    with _match_lock:
        return dict(_match_state)


@router.get("/match/candidates/{product_id}")
def get_candidates(product_id: int, user=Depends(current_user)):
    """상품의 ASIN 매칭 후보 목록."""
    candidates = asin_matching_service.get_candidates(product_id)
    return {"product_id": product_id, "candidates": candidates}


@router.post("/match/select/{product_id}/{asin}")
def select_match(product_id: int, asin: str, user=Depends(current_user)):
    """수동 ASIN 선택/변경."""
    return asin_matching_service.select_asin(product_id, asin)


# ── Offer 등록 ───────────────────────────────────────


@router.post("/offer/validate/{product_id}")
def validate_offer(product_id: int, user=Depends(current_user)):
    """VALIDATION_PREVIEW로 offer 검증."""
    try:
        return offer_registration_service.validate_offer(product_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/offer/register/{product_id}")
def register_offer(
    product_id: int,
    dry_run: bool = True,
    user=Depends(current_user),
):
    """단일 상품 offer 등록."""
    try:
        return offer_registration_service.register_offer(product_id, dry_run=dry_run)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _run_batch_register_bg(limit: int, dry_run: bool):
    with _offer_lock:
        _offer_state.update({
            "running": True, "phase": "register", "current": 0, "total": 0,
            "message": "시작 중", "started_at": datetime.utcnow().isoformat(),
            "finished_at": None, "error": None, "result": None,
        })
    try:
        result = offer_registration_service.batch_register(
            limit=limit,
            dry_run=dry_run,
            progress_cb=_offer_progress_cb,
        )
        with _offer_lock:
            _offer_state.update({
                "running": False, "phase": "done",
                "finished_at": datetime.utcnow().isoformat(),
                "result": {
                    "processed": result["processed"],
                    "success": result["success"],
                    "failed": result["failed"],
                    "dry_run": result["dry_run"],
                },
            })
    except Exception as e:
        with _offer_lock:
            _offer_state.update({
                "running": False, "phase": "error",
                "finished_at": datetime.utcnow().isoformat(),
                "error": str(e),
            })


@router.post("/offer/batch")
def batch_register(
    background: BackgroundTasks,
    limit: int = 10,
    dry_run: bool = True,
    user=Depends(current_user),
):
    """일괄 offer 등록 (백그라운드)."""
    with _offer_lock:
        if _offer_state["running"]:
            raise HTTPException(status_code=409, detail="등록 작업 진행 중")
    background.add_task(_run_batch_register_bg, limit, dry_run)
    return {"status": "started", "limit": limit, "dry_run": dry_run}


@router.get("/offer/progress")
def offer_progress(user=Depends(current_user)):
    """등록 진행 상태 조회."""
    with _offer_lock:
        return dict(_offer_state)


# ── 현황 ─────────────────────────────────────────────


@router.get("/summary")
def pipeline_summary(user=Depends(current_user)):
    """ASIN 파이프라인 현황 요약."""
    return asin_matching_service.get_pipeline_summary()
