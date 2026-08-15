"""
discovery_pipeline.py — 디스커버리 풀 파이프라인 오케스트레이터.

5단계:
  A. categories  추적 카테고리 확인 (비어 있으면 에러로 종료)
  B. rank        비공식 데이터랩 → 카테고리별 TOP 키워드 수집
  C. searchad    네이버 검색광고 → 월간 PC/모바일/경쟁도
  D. trend       공식 datalab/search → 키워드 시계열 → trend_score
  E. cluster     AI 키워드 클러스터링 + 적재

FastAPI BackgroundTask 안에서 동기 실행. asyncio 는 cluster 에서만 asyncio.run().
각 단계는 try/except 로 격리하여 하나가 실패해도 다음 단계로 넘어간다.
진행 상태는 pa_discovery_runs 의 current_stage / stage_log 에 JSON 으로 기록.
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from backend.purchase.database import get_db
from backend.purchase.services import (
    naver_datalab_scraper,
    naver_datalab_service,
    naver_searchad_service,
)
from backend.purchase.services.keyword_cluster_service import (
    cluster_keywords,
    store_clusters,
)

logger = logging.getLogger(__name__)

_COMPETITION_MAP = {"낮음": 0.3, "중간": 0.6, "높음": 0.9}


def _update_run(
    run_id: int,
    *,
    current_stage: Optional[str] = None,
    stage_log: Optional[dict] = None,
    status: Optional[str] = None,
    error: Optional[str] = None,
    inserted_kw: Optional[int] = None,
    updated_kw: Optional[int] = None,
    finished: bool = False,
) -> None:
    fields = []
    params: list = []
    if current_stage is not None:
        fields.append("current_stage=?")
        params.append(current_stage)
    if stage_log is not None:
        fields.append("stage_log=?")
        params.append(json.dumps(stage_log, ensure_ascii=False))
    if status is not None:
        fields.append("status=?")
        params.append(status)
    if error is not None:
        fields.append("error=?")
        params.append(error)
    if inserted_kw is not None:
        fields.append("inserted_kw=?")
        params.append(inserted_kw)
    if updated_kw is not None:
        fields.append("updated_kw=?")
        params.append(updated_kw)
    if finished:
        fields.append("finished_at=CURRENT_TIMESTAMP")
    if not fields:
        return
    params.append(run_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE pa_discovery_runs SET {', '.join(fields)} WHERE id=?",
            tuple(params),
        )


def _load_stage_log(run_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT stage_log FROM pa_discovery_runs WHERE id=?", (run_id,)
        ).fetchone()
    if not row or not row["stage_log"]:
        return {}
    try:
        return json.loads(row["stage_log"])
    except (json.JSONDecodeError, TypeError):
        return {}


def _comp_to_float(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        return _COMPETITION_MAP.get(v.strip())
    return None


# ─── Stage B ──────────────────────────────────────────────────────────
def _stage_rank(run_id: int, tracked: list[dict]) -> list[int]:
    """카테고리별 TOP 키워드 수집 → keywords UPSERT.
    Returns: 이 run 에서 생성/업데이트된 keyword.id 목록.
    """
    log = _load_stage_log(run_id)
    collected_kids: list[int] = []
    inserted = 0
    total_cats = len(tracked)
    for idx, cat in enumerate(tracked, start=1):
        cid = cat["cid"]
        name = cat.get("name", "")
        try:
            ranks = naver_datalab_scraper.fetch_keyword_rank(
                cid, days=30, max_pages=5
            )
        except Exception as e:
            logger.error(f"rank cid={cid} 실패: {e}")
            ranks = []
        for row in ranks:
            kw = (row.get("keyword") or "").strip()
            if not kw:
                continue
            kid = naver_datalab_service.store_keyword(
                kw, source="datalab_rank", category_cid=cid,
            )
            if kid:
                collected_kids.append(kid)
                inserted += 1
        log["rank"] = {
            "current": idx,
            "total": total_cats,
            "category": name,
            "collected": inserted,
        }
        _update_run(run_id, stage_log=log)
    log["rank"]["done"] = True
    _update_run(run_id, stage_log=log, inserted_kw=inserted)
    return collected_kids


# ─── Stage C ──────────────────────────────────────────────────────────
def _stage_searchad(run_id: int) -> list[str]:
    """monthly_pc NULL 이거나 0인 datalab_rank 키워드에 검색량 보강."""
    log = _load_stage_log(run_id)
    with get_db() as conn:
        rows = conn.execute(
            """SELECT keyword FROM keywords
               WHERE source='datalab_rank'
                 AND (monthly_pc IS NULL OR monthly_pc=0)"""
        ).fetchall()
    pending = [r["keyword"] for r in rows]
    total = len(pending)
    done = 0
    updated = 0
    log["searchad"] = {"current": 0, "total": total}
    _update_run(run_id, stage_log=log)

    for i in range(0, total, 5):
        chunk = pending[i:i + 5]
        try:
            vols = naver_searchad_service.get_keyword_volumes(chunk) or []
        except Exception as e:
            logger.error(f"searchad chunk={chunk} 실패: {e}")
            vols = []
        with get_db() as conn:
            for it in vols:
                comp = _comp_to_float(it.get("competition"))
                conn.execute(
                    """UPDATE keywords
                       SET monthly_pc=?, monthly_mobile=?, competition=?
                       WHERE keyword=? AND source='datalab_rank'""",
                    (
                        it.get("monthly_pc", 0),
                        it.get("monthly_mobile", 0),
                        comp,
                        it.get("keyword"),
                    ),
                )
                updated += 1
        done = min(i + len(chunk), total)
        log["searchad"] = {"current": done, "total": total, "updated": updated}
        _update_run(run_id, stage_log=log)

    log["searchad"]["done"] = True
    _update_run(run_id, stage_log=log, updated_kw=updated)
    return pending


# ─── Stage D ──────────────────────────────────────────────────────────
def _stage_trend(run_id: int) -> None:
    """trend_score 가 NULL 인 datalab_rank 키워드 → 시계열 조회 → 점수 계산."""
    log = _load_stage_log(run_id)
    with get_db() as conn:
        rows = conn.execute(
            """SELECT keyword FROM keywords
               WHERE source='datalab_rank' AND trend_score IS NULL"""
        ).fetchall()
    pending = [r["keyword"] for r in rows]
    total = len(pending)
    done = 0
    scored = 0
    log["trend"] = {"current": 0, "total": total}
    _update_run(run_id, stage_log=log)

    for i in range(0, total, 5):
        chunk = pending[i:i + 5]
        try:
            series_map = naver_datalab_service.fetch_keyword_search_trends(
                chunk, days=30
            )
        except Exception as e:
            logger.error(f"trend chunk={chunk} 실패: {e}")
            series_map = {}
        with get_db() as conn:
            for kw in chunk:
                score = naver_datalab_service.compute_trend_score(
                    series_map.get(kw, [])
                )
                if score is None:
                    continue
                conn.execute(
                    """UPDATE keywords SET trend_score=?
                       WHERE keyword=? AND source='datalab_rank'""",
                    (score, kw),
                )
                scored += 1
        done = min(i + len(chunk), total)
        log["trend"] = {"current": done, "total": total, "scored": scored}
        _update_run(run_id, stage_log=log)

    log["trend"]["done"] = True
    _update_run(run_id, stage_log=log)


# ─── Stage E ──────────────────────────────────────────────────────────
def _stage_cluster(run_id: int) -> None:
    log = _load_stage_log(run_id)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT keyword FROM keywords WHERE cluster_id IS NULL LIMIT 80"
        ).fetchall()
    kws = [r["keyword"] for r in rows]
    log["cluster"] = {"input": len(kws), "clusters": 0}
    _update_run(run_id, stage_log=log)
    if not kws:
        log["cluster"]["done"] = True
        _update_run(run_id, stage_log=log)
        return

    try:
        clusters = asyncio.run(cluster_keywords(kws))
        inserted = store_clusters(clusters)
        log["cluster"] = {
            "input": len(kws),
            "clusters": inserted,
            "done": True,
        }
    except Exception as e:
        logger.error(f"cluster 단계 실패: {e}")
        log["cluster"] = {
            "input": len(kws),
            "clusters": 0,
            "done": True,
            "error": str(e),
        }
    _update_run(run_id, stage_log=log)


# ─── Entry point ──────────────────────────────────────────────────────
def run_discovery_pipeline(run_id: int) -> None:
    """BackgroundTask 진입점. pa_discovery_runs 의 row 를 갱신하며 진행."""
    logger.info(f"[discovery run={run_id}] 시작")

    # Stage A: 추적 카테고리 검사
    _update_run(run_id, current_stage="categories", stage_log={})
    with get_db() as conn:
        tracked_rows = conn.execute(
            "SELECT cid, name FROM pa_discovery_categories WHERE tracked=1"
        ).fetchall()
    tracked = [dict(r) for r in tracked_rows]
    if not tracked:
        msg = "추적 카테고리가 없습니다. 설정 페이지에서 선택해 주세요."
        _update_run(
            run_id,
            current_stage="categories",
            status="failed",
            error=msg,
            finished=True,
        )
        logger.warning(f"[discovery run={run_id}] {msg}")
        return

    stage_log = {
        "categories": {"tracked": len(tracked), "done": True},
    }
    _update_run(run_id, stage_log=stage_log)

    # Stage B
    try:
        _update_run(run_id, current_stage="rank")
        _stage_rank(run_id, tracked)
    except Exception as e:
        logger.exception(f"[discovery run={run_id}] rank 단계 실패")
        log = _load_stage_log(run_id)
        log.setdefault("rank", {})["error"] = str(e)
        _update_run(run_id, stage_log=log)

    # Stage C
    try:
        _update_run(run_id, current_stage="searchad")
        _stage_searchad(run_id)
    except Exception as e:
        logger.exception(f"[discovery run={run_id}] searchad 단계 실패")
        log = _load_stage_log(run_id)
        log.setdefault("searchad", {})["error"] = str(e)
        _update_run(run_id, stage_log=log)

    # Stage D
    try:
        _update_run(run_id, current_stage="trend")
        _stage_trend(run_id)
    except Exception as e:
        logger.exception(f"[discovery run={run_id}] trend 단계 실패")
        log = _load_stage_log(run_id)
        log.setdefault("trend", {})["error"] = str(e)
        _update_run(run_id, stage_log=log)

    # Stage E
    try:
        _update_run(run_id, current_stage="cluster")
        _stage_cluster(run_id)
    except Exception as e:
        logger.exception(f"[discovery run={run_id}] cluster 단계 실패")
        log = _load_stage_log(run_id)
        log.setdefault("cluster", {})["error"] = str(e)
        _update_run(run_id, stage_log=log)

    _update_run(run_id, current_stage="done", status="done", finished=True)
    logger.info(f"[discovery run={run_id}] 완료")
