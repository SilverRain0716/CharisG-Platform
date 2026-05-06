"""
coupang_order_poller.py — 쿠팡 주문 1시간 폴링.

lifespan에서 asyncio.create_task로 기동. 매 POLL_INTERVAL_SEC 마다
지정한 KST 날짜 범위(오늘 + 어제)로 쿠팡 ordersheet를 읽어 orders 테이블에 upsert.
쿠팡 ordersheet API는 yyyy-MM-dd 단위만 받으므로 시간 증분 대신 날짜로 동작 —
UNIQUE(channel, channel_order_id) 제약으로 중복 호출은 idempotent.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from backend.purchase.database import get_db
from backend.purchase.services.coupang_service import sync_orders
from backend.purchase.services.order_translator import translate_order

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
POLL_INTERVAL_SEC = 3600  # 1시간
INITIAL_DELAY_SEC = 60
# 매 폴링마다 조회할 과거 날짜 수 (오늘 포함). 2이면 오늘+어제.
POLL_DAYS = 2


def _format_kst_date(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%Y-%m-%d")


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_job(
    job_id: str, start: str, end: str, result: dict, status: str = "done", error: str | None = None
) -> None:
    msg = (
        f"폴링 [{start} ~ {end}] — 조회 {result.get('fetched', 0)}, "
        f"신규 {result.get('inserted', 0)}, 중복 {result.get('duplicated', 0)}, "
        f"매핑실패 {result.get('unmapped', 0)}, 에러 {result.get('errors', 0)}"
    )
    if error:
        msg = f"폴링 실패 [{start} ~ {end}] — {error}"
    ts = _now_iso_utc()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO batch_jobs (id, job_type, status, total, processed, errors,
                phase_message, error_message, created_at, started_at, finished_at)
               VALUES (?, 'coupang_order_sync', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                status,
                result.get("fetched", 0),
                result.get("inserted", 0),
                result.get("errors", 0),
                msg,
                error,
                ts,
                ts,
                ts,
            ),
        )


async def _poll_once() -> None:
    """1회 폴링 실행. 예외는 내부에서 삼켜 루프 지속성 보장.

    INSERT(sync_orders) 성공 시 audit log(_record_job) 보다 번역 task 큐잉을 먼저
    수행한다. _record_job 은 단순 audit 이지만 SQLite 락 경합으로 실패할 수 있고,
    같은 try 블록에 두면 INSERT 후 신규 주문이 번역 큐에 못 들어가는 사고가 난다
    (2026-05-03 우혜경 주문 67번 사고).
    """
    now = datetime.now(tz=KST)
    start = _format_kst_date(now - timedelta(days=POLL_DAYS - 1))
    end = _format_kst_date(now)
    job_id = uuid.uuid4().hex[:12]

    try:
        result = await asyncio.to_thread(sync_orders, start, end)
    except Exception as e:
        logger.exception("[coupang-order-poller] sync_orders 실패")
        try:
            _record_job(
                job_id, start, end, {"fetched": 0, "inserted": 0, "errors": 1},
                status="error", error=str(e)[:500],
            )
        except Exception:
            logger.exception("[coupang-order-poller] _record_job 도 실패 (무시)")
        return

    # 신규 주문 번역 큐잉을 audit log 보다 먼저 실행 — _record_job 실패가 번역 누락을 일으키지 않게
    for new_oid in result.get("new_order_ids", []):
        asyncio.create_task(_translate_safely(new_oid))

    logger.info(
        "[coupang-order-poller] %s~%s — 조회=%d 신규=%d 중복=%d 매핑실패=%d 에러=%d",
        start, end,
        result.get("fetched", 0),
        result.get("inserted", 0),
        result.get("duplicated", 0),
        result.get("unmapped", 0),
        result.get("errors", 0),
    )

    try:
        _record_job(job_id, start, end, result, status="done")
    except Exception:
        logger.exception("[coupang-order-poller] _record_job 실패 (audit 만 누락, 번역은 진행됨)")


async def _translate_safely(order_id: int) -> None:
    """번역 실패해도 루프에 영향 없게 예외 삼킴."""
    try:
        await translate_order(order_id)
    except Exception:
        logger.exception("[coupang-order-poller] order %d 번역 태스크 예외", order_id)


async def run_forever() -> None:
    """lifespan에서 create_task로 시작하는 메인 루프."""
    await asyncio.sleep(INITIAL_DELAY_SEC)
    while True:
        try:
            await _poll_once()
        except asyncio.CancelledError:
            logger.info("[coupang-order-poller] 취소됨")
            raise
        except Exception:
            logger.exception("[coupang-order-poller] 루프 내 예외 — 계속 진행")
        await asyncio.sleep(POLL_INTERVAL_SEC)
