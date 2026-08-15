"""pipeline router — streaming pipeline 모니터링 API.

GET /api/pa/pipeline/health           — 전체 상태 종합
GET /api/pa/pipeline/stages           — stage 별 카운트
GET /api/pa/pipeline/queue-depths     — 큐 깊이 (backpressure 판정용)
GET /api/pa/pipeline/stuck            — stuck items 리스트
GET /api/pa/pipeline/throughput       — 시간당 처리량
GET /api/pa/pipeline/db-health        — DB lock 측정
GET /api/pa/pipeline/sheet/{sid}      — 특정 시트 진행
"""
from fastapi import APIRouter, Depends

from backend.purchase.auth import current_user
from backend.purchase.database import get_db

router = APIRouter(prefix="/api/pa/pipeline", tags=["pa-pipeline"])

# stage 순서 (FSM)
STAGE_ORDER = [
    "imported",
    "promote_done",
    "ai_done",
    "channel_send_done",
    "upload_done",
    "kr_verify_done",
    "forwarder_done",
    "coupon_done",
    "done",
    "failed",
    "excluded",
]

# backpressure threshold (config — STAGES dict 와 일치)
BACKPRESSURE_THRESHOLDS = {
    "imported": 0,  # promote 가 처리
    "promote_done": 1000,
    "ai_done": 2000,
    "channel_send_done": 500,
    "upload_done": 3000,
    "kr_verify_done": 5000,
    "forwarder_done": 5000,
    "coupon_done": 5000,
}


@router.get("/stages")
def stage_counts(user: dict = Depends(current_user)) -> dict:
    """stage 별 row 카운트."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT stage, COUNT(*) AS cnt FROM pipeline_items GROUP BY stage"""
        ).fetchall()
    by_stage = {s: 0 for s in STAGE_ORDER}
    for r in rows:
        by_stage[r["stage"]] = r["cnt"]
    return {"by_stage": by_stage, "total": sum(by_stage.values())}


@router.get("/queue-depths")
def queue_depths(user: dict = Depends(current_user)) -> dict:
    """다음 stage 큐 깊이 — backpressure 판정용."""
    counts = stage_counts(user)["by_stage"]
    depths = []
    bottleneck = None
    max_ratio = 0.0
    for stage, threshold in BACKPRESSURE_THRESHOLDS.items():
        if threshold == 0:
            continue
        depth = counts.get(stage, 0)
        ratio = depth / threshold
        if ratio > max_ratio:
            max_ratio = ratio
            bottleneck = stage
        depths.append({
            "stage": stage,
            "depth": depth,
            "threshold": threshold,
            "ratio": round(ratio, 2),
        })
    return {
        "depths": depths,
        "bottleneck_stage": bottleneck,
        "bottleneck_ratio": round(max_ratio, 2),
    }


@router.get("/stuck")
def stuck_items(limit: int = 50, user: dict = Depends(current_user)) -> dict:
    """stuck items (30분 이상 정체)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM vw_pipeline_stuck ORDER BY stuck_min DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return {
        "count": len(rows),
        "items": [dict(r) for r in rows],
    }


@router.get("/throughput")
def throughput(user: dict = Depends(current_user)) -> dict:
    """최근 1h 처리량 (pipeline_metrics 기반)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM vw_pipeline_throughput"""
        ).fetchall()
    return {"by_stage": [dict(r) for r in rows]}


@router.get("/db-health")
def db_health(user: dict = Depends(current_user)) -> dict:
    """DB lock 충돌 측정."""
    with get_db() as conn:
        rows = conn.execute("""SELECT * FROM vw_db_lock_health""").fetchall()
    # 위험 신호: max_wait_ms > 5000 또는 avg_wait_ms > 500
    alerts = []
    for r in rows:
        if r["max_wait_ms"] and r["max_wait_ms"] > 5000:
            alerts.append(f"{r['stage']}: max_wait_ms={r['max_wait_ms']} (>5000)")
        if r["avg_wait_ms"] and r["avg_wait_ms"] > 500:
            alerts.append(f"{r['stage']}: avg_wait_ms={round(r['avg_wait_ms'],1)} (>500)")
    return {
        "by_stage": [dict(r) for r in rows],
        "alerts": alerts,
    }


@router.get("/sheet/{sid}")
def sheet_progress(sid: int, user: dict = Depends(current_user)) -> dict:
    """특정 시트의 pipeline_items 진행 상황."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT stage, COUNT(*) AS cnt, MIN(last_progress_at) AS oldest
                 FROM pipeline_items WHERE sheet_queue_id = ? GROUP BY stage""",
            (sid,),
        ).fetchall()
        sheet_meta = conn.execute(
            """SELECT id, status, current_step, imported, sheet_label, queued_at
                 FROM sheet_queue WHERE id = ?""",
            (sid,),
        ).fetchone()
    return {
        "sheet": dict(sheet_meta) if sheet_meta else None,
        "by_stage": [dict(r) for r in rows],
        "total": sum(r["cnt"] for r in rows),
    }


@router.get("/health")
def pipeline_health(user: dict = Depends(current_user)) -> dict:
    """전체 종합 — 한 endpoint 에서 핵심 지표 다 조회."""
    stages = stage_counts(user)
    depths = queue_depths(user)
    stuck = stuck_items(limit=10, user=user)
    tp = throughput(user)
    db = db_health(user)
    return {
        "summary": {
            "total": stages["total"],
            "done": stages["by_stage"]["done"],
            "failed": stages["by_stage"]["failed"],
            "excluded": stages["by_stage"]["excluded"],
            "in_progress": (
                stages["total"]
                - stages["by_stage"]["done"]
                - stages["by_stage"]["failed"]
                - stages["by_stage"]["excluded"]
            ),
        },
        "by_stage": stages["by_stage"],
        "queue_depths": depths,
        "stuck_count": stuck["count"],
        "stuck_top": stuck["items"][:5],
        "throughput_1h": tp["by_stage"],
        "db_health": db,
    }
