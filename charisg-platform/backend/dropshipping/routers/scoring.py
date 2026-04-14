"""DS Scoring — 3×3 히트맵, 분포, 실행, 이력."""
import threading
import time as _time
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends

from backend.dropshipping.auth import current_user
from backend.dropshipping.database import get_db
from backend.dropshipping.services import scoring_service

router = APIRouter(prefix="/api/ds/scoring", tags=["ds-scoring"])

# 파이프라인 진행 상태 (in-memory)
_pipeline_state: dict = {
    "running": False,
    "phase": "idle",      # idle | collect | score | done | error
    "current": 0,
    "total": 0,
    "message": "",
    "started_at": None,
    "finished_at": None,
    "error": None,
}
_pipeline_lock = threading.Lock()


def _progress_cb(phase: str, current: int, total: int, message: str):
    with _pipeline_lock:
        _pipeline_state["phase"] = phase
        _pipeline_state["current"] = current
        _pipeline_state["total"] = total
        _pipeline_state["message"] = message


def _run_pipeline_bg(collect_cj: bool, use_trends: bool):
    with _pipeline_lock:
        _pipeline_state.update({
            "running": True, "phase": "collect", "current": 0, "total": 0,
            "message": "시작 중", "started_at": datetime.utcnow().isoformat(),
            "finished_at": None, "error": None,
        })
    try:
        scoring_service.run_scoring_pipeline(
            use_trends=use_trends,
            collect_cj=collect_cj,
            progress_cb=_progress_cb,
        )
        with _pipeline_lock:
            _pipeline_state.update({
                "running": False, "phase": "done",
                "message": "완료", "finished_at": datetime.utcnow().isoformat(),
            })
    except Exception as e:
        with _pipeline_lock:
            _pipeline_state.update({
                "running": False, "phase": "error", "error": str(e),
                "finished_at": datetime.utcnow().isoformat(),
            })


@router.get("/matrix")
def get_matrix(user: dict = Depends(current_user)):
    """3×3 매트릭스 히트맵 데이터 (Demand × Margin)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT matrix_group, COUNT(*) c,
                      SUM(CASE WHEN go_decision IN ('GO','GO_ORGANIC') THEN 1 ELSE 0 END) go_count
               FROM collected_products
               WHERE hard_filter_pass=1 AND matrix_group IS NOT NULL
               GROUP BY matrix_group"""
        ).fetchall()
    matrix = {r["matrix_group"]: {"count": r["c"], "go_count": r["go_count"]} for r in rows}
    cells = []
    for d in ("A", "B", "C"):
        for m in ("A", "B", "C"):
            key = d + m
            cell = matrix.get(key, {"count": 0, "go_count": 0})
            cells.append({
                "demand": d,
                "margin": m,
                "key": key,
                "count": cell["count"],
                "go_count": cell["go_count"],
                "go_ratio": round(cell["go_count"] / cell["count"] * 100, 1) if cell["count"] else 0,
            })
    return {"cells": cells}


@router.get("/distribution")
def get_distribution(user: dict = Depends(current_user)):
    """Demand / Gap / Margin 분포 히스토그램."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT demand_score, gap_score, margin_score
               FROM collected_products WHERE hard_filter_pass=1"""
        ).fetchall()
    def _bins(values, bin_size=0.1):
        buckets = {}
        for v in values:
            if v is None:
                continue
            b = round(v // bin_size * bin_size, 2)
            buckets[b] = buckets.get(b, 0) + 1
        return [{"bin": k, "count": v} for k, v in sorted(buckets.items())]
    return {
        "demand": _bins([r["demand_score"] for r in rows]),
        "gap":    _bins([r["gap_score"]    for r in rows]),
        "margin": _bins([r["margin_score"] for r in rows]),
    }


@router.get("/filter-fails")
def get_filter_fails(user: dict = Depends(current_user)):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT filter_fail_reason reason, COUNT(*) c
               FROM collected_products
               WHERE hard_filter_pass=0 AND filter_fail_reason IS NOT NULL
               GROUP BY filter_fail_reason ORDER BY c DESC"""
        ).fetchall()
    return [{"reason": r["reason"], "count": r["c"]} for r in rows]


@router.post("/run")
def run_scoring(background: BackgroundTasks, collect_cj: bool = True,
                use_trends: bool = True, user: dict = Depends(current_user)):
    """스펙 기준 통합 파이프라인: CJ 수집 → Hard Filter → 3축 스코어링."""
    with _pipeline_lock:
        if _pipeline_state["running"]:
            return {"started": False, "message": "이미 파이프라인 실행 중", "state": dict(_pipeline_state)}
    background.add_task(_run_pipeline_bg, collect_cj, use_trends)
    return {"started": True, "message": "CJ 수집 + 스코어링 파이프라인 백그라운드 실행 시작"}


@router.get("/progress")
def get_progress(user: dict = Depends(current_user)):
    """파이프라인 진행 상태 폴링."""
    with _pipeline_lock:
        return dict(_pipeline_state)


@router.get("/report")
def get_report(user: dict = Depends(current_user)):
    return scoring_service.get_scoring_report()


@router.get("/history")
def get_history(user: dict = Depends(current_user)):
    """간단한 실행 이력 — 마지막 sort_score 갱신 시각 기준."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT updated_at, COUNT(*) c, AVG(sort_score) avg_score
               FROM collected_products WHERE sort_score IS NOT NULL
               GROUP BY date(updated_at) ORDER BY updated_at DESC LIMIT 30"""
        ).fetchall()
    return [
        {"date": r["updated_at"][:10] if r["updated_at"] else "", "count": r["c"],
         "avg_score": round(r["avg_score"] or 0, 3)}
        for r in rows
    ]
