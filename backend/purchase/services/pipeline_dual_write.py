"""pipeline_dual_write — sheet_queue_worker 가 각 단계 끝나면 호출하는 헬퍼.

streaming pipeline Phase 1 — 기존 batch sheet_queue_worker 흐름 유지하면서
pipeline_items 테이블에도 stage 진행을 이중 쓰기. Phase 2+ 에서 streaming
worker 로 점진 전환.

함수 모두 idempotent (재호출 안전). 에러는 catch 후 로그만 — 절대 본 흐름 차단 X.
"""
import logging
from datetime import datetime, timezone
from typing import Iterable, Optional

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(fn):
    """본 흐름 차단 방지 wrapper. 예외 catch 후 로그만."""

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.exception(f"[pipeline-dual] {fn.__name__} 실패 — 무시")
            return None

    return wrapper


@_safe
def insert_items_for_sheet(
    sheet_queue_id: int,
    asin_to_sc_id: dict,
) -> int:
    """시트 import 직후 sourcing_candidates 신규 ASIN 을 pipeline_items 에 INSERT.

    Args:
        sheet_queue_id: sheet_queue.id
        asin_to_sc_id: {asin: sourcing_candidate_id} 매핑
    Returns:
        INSERT 한 row 수
    """
    if not asin_to_sc_id:
        return 0
    now = _now()
    rows = [(sheet_queue_id, asin, sc_id, "imported", now) for asin, sc_id in asin_to_sc_id.items()]
    with get_db() as conn:
        cur = conn.executemany(
            """INSERT OR IGNORE INTO pipeline_items
                   (sheet_queue_id, asin, sourcing_candidate_id, stage, last_progress_at)
               VALUES (?, ?, ?, ?, ?)""",
            rows,
        )
        inserted = cur.rowcount
        conn.commit()
    logger.info(
        f"[pipeline-dual] insert sheet_queue_id={sheet_queue_id} "
        f"asin={len(asin_to_sc_id)} inserted={inserted}"
    )
    return inserted


@_safe
def advance_stage(
    sheet_queue_id: int,
    new_stage: str,
    product_ids: Optional[Iterable[int]] = None,
    asins: Optional[Iterable[str]] = None,
    listing_ids: Optional[Iterable[int]] = None,
    error_message: Optional[str] = None,
) -> int:
    """주어진 sid 의 매칭 row 들의 stage 를 새 stage 로 advance.

    매칭 조건 (우선순위):
      1. product_ids (가장 정확)
      2. listing_ids
      3. asins
      4. 위 모두 None → sheet_queue_id 전체

    new_stage 가 'done'/'failed' 면 종료. 그 외는 중간 단계.
    """
    now = _now()
    set_clause = ["stage = ?", "last_progress_at = ?", "stage_started_at = ?"]
    params: list = [new_stage, now, now]
    if error_message:
        set_clause.append("error_message = ?")
        params.append(error_message[:500])
    else:
        set_clause.append("error_message = NULL")

    where_clause = ["sheet_queue_id = ?"]
    where_params: list = [sheet_queue_id]
    if product_ids:
        pids = list(product_ids)
        if not pids:
            return 0
        ph = ",".join("?" * len(pids))
        where_clause.append(f"product_id IN ({ph})")
        where_params.extend(pids)
    elif listing_ids:
        lids = list(listing_ids)
        if not lids:
            return 0
        ph = ",".join("?" * len(lids))
        where_clause.append(f"listing_id IN ({ph})")
        where_params.extend(lids)
    elif asins:
        a_list = list(asins)
        if not a_list:
            return 0
        ph = ",".join("?" * len(a_list))
        where_clause.append(f"asin IN ({ph})")
        where_params.extend(a_list)

    sql = f"""UPDATE pipeline_items
                 SET {", ".join(set_clause)}
               WHERE {" AND ".join(where_clause)}"""
    with get_db() as conn:
        cur = conn.execute(sql, params + where_params)
        updated = cur.rowcount
        conn.commit()
    logger.info(
        f"[pipeline-dual] advance sid={sheet_queue_id} stage={new_stage} "
        f"updated={updated}"
    )
    return updated


@_safe
def set_product_id(sheet_queue_id: int, asin_to_pid: dict) -> int:
    """promote 단계 끝나면 asin → product_id 매핑 추가."""
    if not asin_to_pid:
        return 0
    now = _now()
    with get_db() as conn:
        updated = 0
        for asin, pid in asin_to_pid.items():
            cur = conn.execute(
                """UPDATE pipeline_items
                       SET product_id = ?, last_progress_at = ?
                     WHERE sheet_queue_id = ? AND asin = ?""",
                (pid, now, sheet_queue_id, asin),
            )
            updated += cur.rowcount
        conn.commit()
    logger.info(
        f"[pipeline-dual] set_product_id sid={sheet_queue_id} updated={updated}"
    )
    return updated


@_safe
def set_listing_id(sheet_queue_id: int, pid_to_listing: dict) -> int:
    """channel_send 단계 끝나면 product_id → listing_id 매핑."""
    if not pid_to_listing:
        return 0
    now = _now()
    with get_db() as conn:
        updated = 0
        for pid, lid in pid_to_listing.items():
            cur = conn.execute(
                """UPDATE pipeline_items
                       SET listing_id = ?, last_progress_at = ?
                     WHERE sheet_queue_id = ? AND product_id = ?""",
                (lid, now, sheet_queue_id, pid),
            )
            updated += cur.rowcount
        conn.commit()
    logger.info(
        f"[pipeline-dual] set_listing_id sid={sheet_queue_id} updated={updated}"
    )
    return updated
