"""DS ASIN Pipeline — 마켓별 ASIN 매칭 + offer 등록 API."""
import threading
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from backend.dropshipping.auth import current_user
from backend.dropshipping.services import asin_matching_service, offer_registration_service

router = APIRouter(prefix="/api/ds/asin-pipeline", tags=["ds-asin-pipeline"])

# ── 매칭/등록 상태 (in-memory) ──────────────────

_match_state: dict = {
    "running": False, "phase": "idle", "current": 0, "total": 0,
    "message": "", "started_at": None, "finished_at": None,
    "error": None, "result": None,
}
_match_lock = threading.Lock()

_offer_state: dict = {
    "running": False, "phase": "idle", "current": 0, "total": 0,
    "message": "", "started_at": None, "finished_at": None,
    "error": None, "result": None,
}
_offer_lock = threading.Lock()


def _match_progress_cb(phase, current, total, message):
    with _match_lock:
        _match_state.update({"phase": phase, "current": current, "total": total, "message": message})


def _offer_progress_cb(phase, current, total, message):
    with _offer_lock:
        _offer_state.update({"phase": phase, "current": current, "total": total, "message": message})


# ── ASIN 매칭 ────────────────────────────────────────

@router.post("/match/single/{product_id}")
def match_single(product_id: int, market: str = Query(default="US"), user=Depends(current_user)):
    try:
        best = asin_matching_service.find_best_match(product_id, market=market)
        candidates = asin_matching_service.get_candidates(product_id, market=market)
        return {"product_id": product_id, "market": market, "best_match": best, "candidates": candidates}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


def _run_batch_match_bg(limit, min_sort_score, market):
    with _match_lock:
        _match_state.update({
            "running": True, "phase": "match", "current": 0, "total": 0,
            "message": f"[{market}] 시작 중", "started_at": datetime.utcnow().isoformat(),
            "finished_at": None, "error": None, "result": None,
        })
    try:
        result = asin_matching_service.batch_match(
            limit=limit, min_sort_score=min_sort_score, market=market,
            progress_cb=_match_progress_cb,
        )
        with _match_lock:
            _match_state.update({
                "running": False, "phase": "done",
                "finished_at": datetime.utcnow().isoformat(),
                "result": {k: result[k] for k in ("processed", "matched", "no_match", "failed")},
            })
    except Exception as e:
        with _match_lock:
            _match_state.update({"running": False, "phase": "error",
                                 "finished_at": datetime.utcnow().isoformat(), "error": str(e)})


@router.post("/match/batch")
def match_batch(background: BackgroundTasks, limit: int = 50,
                min_sort_score: float = 0.0, market: str = Query(default="US"),
                user=Depends(current_user)):
    with _match_lock:
        if _match_state["running"]:
            raise HTTPException(status_code=409, detail="매칭 작업 진행 중")
    background.add_task(_run_batch_match_bg, limit, min_sort_score, market)
    return {"status": "started", "limit": limit, "market": market}


@router.get("/match/progress")
def match_progress(user=Depends(current_user)):
    with _match_lock:
        return dict(_match_state)


@router.get("/match/candidates/{product_id}")
def get_candidates(product_id: int, market: str = Query(default="US"), user=Depends(current_user)):
    candidates = asin_matching_service.get_candidates(product_id, market=market)
    return {"product_id": product_id, "market": market, "candidates": candidates}


@router.post("/match/select/{product_id}/{asin}")
def select_match(product_id: int, asin: str, market: str = Query(default="US"),
                 user=Depends(current_user)):
    return asin_matching_service.select_asin(product_id, asin, market=market)


# ── Offer 등록 ───────────────────────────────────────

@router.post("/offer/validate/{product_id}")
def validate_offer(product_id: int, market: str = Query(default="US"), user=Depends(current_user)):
    try:
        return offer_registration_service.validate_offer(product_id, market=market)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/offer/register/{product_id}")
def register_offer(product_id: int, dry_run: bool = True,
                   market: str = Query(default="US"), user=Depends(current_user)):
    try:
        return offer_registration_service.register_offer(product_id, market=market, dry_run=dry_run)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _run_batch_register_bg(limit, market, dry_run):
    with _offer_lock:
        _offer_state.update({
            "running": True, "phase": "register", "current": 0, "total": 0,
            "message": "시작 중", "started_at": datetime.utcnow().isoformat(),
            "finished_at": None, "error": None, "result": None,
        })
    try:
        result = offer_registration_service.batch_register(
            limit=limit, market=market, dry_run=dry_run, progress_cb=_offer_progress_cb,
        )
        with _offer_lock:
            _offer_state.update({
                "running": False, "phase": "done",
                "finished_at": datetime.utcnow().isoformat(),
                "result": {k: result[k] for k in ("processed", "success", "failed", "dry_run")},
            })
    except Exception as e:
        with _offer_lock:
            _offer_state.update({"running": False, "phase": "error",
                                 "finished_at": datetime.utcnow().isoformat(), "error": str(e)})


@router.post("/offer/batch")
def batch_register(background: BackgroundTasks, limit: int = 10,
                   dry_run: bool = True, market: str = Query(default="US"),
                   user=Depends(current_user)):
    with _offer_lock:
        if _offer_state["running"]:
            raise HTTPException(status_code=409, detail="등록 작업 진행 중")
    background.add_task(_run_batch_register_bg, limit, market, dry_run)
    return {"status": "started", "limit": limit, "market": market, "dry_run": dry_run}


@router.get("/offer/progress")
def offer_progress(user=Depends(current_user)):
    with _offer_lock:
        return dict(_offer_state)


# ── 현황 ─────────────────────────────────────────────

@router.get("/summary")
def pipeline_summary(market: str = Query(default="US"), user=Depends(current_user)):
    return asin_matching_service.get_pipeline_summary(market=market)
