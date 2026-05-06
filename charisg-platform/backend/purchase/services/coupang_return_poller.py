"""
coupang_return_poller.py — 쿠팡 반품/취소 30분 폴링.

쿠팡 v6 returnRequests 를 두 번 호출해서 cancel signal 을 모두 캐치:
  1. cancelType=CANCEL (status 없음, orderId 없음) — 결제완료 즉시취소
  2. cancelType=RETURN, status=RU (출고중지요청), UC (반품접수) — 발송 후

매칭되는 orders 행 찾아 cancel_* 컬럼 + current_step='cancelled' 업데이트.

lifespan 에서 asyncio.create_task(run_forever()).
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from backend.purchase.database import get_db, get_db_hot
from backend.purchase.services.coupang_service import get_return_requests

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
POLL_INTERVAL_SEC = 1800  # 30분
INITIAL_DELAY_SEC = 90
LOOKBACK_DAYS = 14        # 매 폴링마다 14일 과거까지 조회


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_kst_date(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%Y-%m-%d")


def _record_job(job_id: str, summary: dict, *, status: str = "done", error: str | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO batch_jobs
               (id, job_type, status, total, processed, errors, phase_message, started_at, finished_at, created_at)
               VALUES (?, 'coupang_return_sync', ?, ?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))""",
            (job_id, status,
             summary.get("total", 0), summary.get("matched", 0), summary.get("unmatched", 0),
             error or f"CANCEL:{summary.get('cancel', 0)} RETURN_RU:{summary.get('ru', 0)} RETURN_UC:{summary.get('uc', 0)} matched:{summary.get('matched', 0)}"),
        )


def _apply_return_data(items: list[dict], cancel_type: str) -> tuple[int, int]:
    """orders 테이블에 cancel 정보 반영. (matched, unmatched).

    매칭 키: orders.channel='coupang' AND channel_order_id=str(orderId).
    """
    if not items:
        return 0, 0
    matched = 0
    unmatched = 0
    with get_db_hot() as conn:
        for it in items:
            order_id = it.get("orderId")
            receipt_id = it.get("receiptId")
            receipt_status = it.get("receiptStatus")  # RU/UC/CC/PR/...
            created_at = it.get("createdAt") or ""
            reason_text = (it.get("reasonCodeText") or it.get("cancelReason") or "")[:200]
            cancel_count_sum = it.get("cancelCountSum") or 0
            release_stop = it.get("releaseStopStatus") or ""

            if not order_id:
                unmatched += 1
                continue

            cur = conn.execute(
                "SELECT id, current_step, canceled, cancel_receipt_id FROM orders WHERE channel='coupang' AND channel_order_id=?",
                (str(order_id),),
            ).fetchone()
            if not cur:
                unmatched += 1
                continue

            # cancel 확정 판단:
            # - receiptStatus = RETURNS_COMPLETED (CC) → 완전 cancel
            # - releaseStopStatus = '처리(출고중지)' or '자동처리(이미출고)' → 처리 완료
            # - cancelType=CANCEL 면 즉시취소
            is_full_cancel = (
                cancel_type == "CANCEL"
                or receipt_status == "RETURNS_COMPLETED"
                or release_stop in ("처리(출고중지)", "자동처리(이미출고)")
            )
            new_step = "cancelled" if is_full_cancel else cur["current_step"]

            conn.execute(
                """UPDATE orders SET
                   canceled = CASE WHEN ? = 1 THEN 1 ELSE canceled END,
                   cancel_count = ?,
                   cancel_receipt_id = ?,
                   cancel_status = ?,
                   cancel_reason = ?,
                   cancel_at = COALESCE(cancel_at, ?),
                   cancel_type = ?,
                   current_step = ?
                   WHERE id=?""",
                (
                    1 if is_full_cancel else 0,
                    int(cancel_count_sum) if cancel_count_sum else cur["canceled"] if False else 0,  # cancel_count
                    int(receipt_id) if receipt_id else None,
                    receipt_status,
                    reason_text,
                    created_at,
                    cancel_type,  # CANCEL or RETURN
                    new_step,
                    cur["id"],
                ),
            )
            matched += 1
    return matched, unmatched


async def _poll_once() -> dict:
    """한 번 polling — CANCEL + RETURN(RU/UC). 결과 dict 반환."""
    now_kst = datetime.now(KST)
    end_str = _format_kst_date(now_kst)
    start_str = _format_kst_date(now_kst - timedelta(days=LOOKBACK_DAYS))

    summary = {"cancel": 0, "ru": 0, "uc": 0, "matched": 0, "unmatched": 0, "total": 0}

    # 1) CANCEL (즉시취소)
    try:
        body = await asyncio.to_thread(
            get_return_requests,
            start_str, end_str, status=None, cancel_type="CANCEL",
            search_type=None, max_per_page=50,
        )
        if body:
            data = body.get("data") or []
            summary["cancel"] = len(data)
            m, um = _apply_return_data(data, "CANCEL")
            summary["matched"] += m
            summary["unmatched"] += um
            summary["total"] += len(data)
    except Exception as e:
        logger.exception(f"[coupang-return-poller] CANCEL polling 예외: {e}")

    # 2) RETURN status=RU (출고중지요청)
    try:
        body = await asyncio.to_thread(
            get_return_requests,
            start_str, end_str, status="RU", cancel_type="RETURN",
            search_type=None, max_per_page=50,
        )
        if body:
            data = body.get("data") or []
            summary["ru"] = len(data)
            m, um = _apply_return_data(data, "RETURN")
            summary["matched"] += m
            summary["unmatched"] += um
            summary["total"] += len(data)
    except Exception as e:
        logger.exception(f"[coupang-return-poller] RETURN/RU polling 예외: {e}")

    # 3) RETURN status=UC (반품접수)
    try:
        body = await asyncio.to_thread(
            get_return_requests,
            start_str, end_str, status="UC", cancel_type="RETURN",
            search_type=None, max_per_page=50,
        )
        if body:
            data = body.get("data") or []
            summary["uc"] = len(data)
            m, um = _apply_return_data(data, "RETURN")
            summary["matched"] += m
            summary["unmatched"] += um
            summary["total"] += len(data)
    except Exception as e:
        logger.exception(f"[coupang-return-poller] RETURN/UC polling 예외: {e}")

    return summary


async def run_forever() -> None:
    logger.info(f"[coupang-return-poller] 기동 (interval={POLL_INTERVAL_SEC}s, lookback={LOOKBACK_DAYS}d)")
    await asyncio.sleep(INITIAL_DELAY_SEC)
    while True:
        job_id = uuid.uuid4().hex[:12]
        try:
            summary = await _poll_once()
            logger.info(
                f"[coupang-return-poller] CANCEL={summary['cancel']} "
                f"RU={summary['ru']} UC={summary['uc']} "
                f"matched={summary['matched']} unmatched={summary['unmatched']}"
            )
            _record_job(job_id, summary)
        except asyncio.CancelledError:
            logger.info("[coupang-return-poller] 취소됨")
            raise
        except Exception as e:
            logger.exception(f"[coupang-return-poller] 예외: {e}")
            try:
                _record_job(job_id, {"total": 0, "matched": 0, "unmatched": 0}, status="error", error=str(e)[:300])
            except Exception:
                pass
        await asyncio.sleep(POLL_INTERVAL_SEC)
