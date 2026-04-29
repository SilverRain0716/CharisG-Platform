"""
smartstore_order_poller.py — 네이버(스마트스토어) 주문 1시간 폴링.

쿠팡 폴러(coupang_order_poller.py)와 동일 패턴:
  lifespan 에서 asyncio.create_task 로 기동. 매 POLL_INTERVAL_SEC 마다 KST
  지정 기간으로 변경된 주문(PAYED) 을 받아 orders 테이블에 upsert.

네이버 API 는 ISO8601+offset 시각으로 lastChangedFrom 을 받기 때문에 마지막
폴링 시각을 이어가는 게 정확하나, 안전 차원에서 매번 (POLL_DAYS - 1)일 만큼
과거를 다시 훑는다 — UNIQUE(channel, channel_order_id) 로 idempotent.
"""
import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from backend.purchase.database import get_db
from backend.purchase.services.smartstore_order_sync import sync_orders
from backend.purchase.services.order_translator import translate_order

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
POLL_INTERVAL_SEC = int(os.environ.get("SMARTSTORE_ORDER_POLL_SEC", "3600"))
INITIAL_DELAY_SEC = 90
# 네이버 lastChangedFrom 은 24h 이내 윈도우만 허용 — 넘으면 일부 응답이 빈 결과로 떨어진다
# (2026-04-27 신규 주문 누락 incident). 안전하게 23시간으로 제한.
POLL_WINDOW_HOURS = 23


def _format_kst_iso(dt: datetime) -> str:
    """네이버 API 가 받는 형식: 2026-04-25T00:00:00.000+09:00"""
    return dt.astimezone(KST).strftime("%Y-%m-%dT%H:%M:%S.000+09:00")


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_job(
    job_id: str, start: str, end: str, result: dict,
    status: str = "done", error: str | None = None
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
               VALUES (?, 'smartstore_order_sync', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id, status,
                result.get("fetched", 0),
                result.get("inserted", 0),
                result.get("errors", 0),
                msg, error, ts, ts, ts,
            ),
        )


async def _poll_once() -> None:
    """1회 폴링. 예외는 내부에서 흡수해 루프 지속성 보장."""
    now = datetime.now(tz=KST)
    start_dt = now - timedelta(hours=POLL_WINDOW_HOURS)
    start = _format_kst_iso(start_dt)
    end = _format_kst_iso(now)
    job_id = uuid.uuid4().hex[:12]

    try:
        result = await asyncio.to_thread(
            sync_orders, start, end, "PAYED"
        )
        _record_job(job_id, start, end, result, status="done")
        logger.info(
            "[smartstore-order-poller] %s ~ %s — 조회=%d 신규=%d 중복=%d 매핑실패=%d 에러=%d",
            start, end,
            result.get("fetched", 0),
            result.get("inserted", 0),
            result.get("duplicated", 0),
            result.get("unmapped", 0),
            result.get("errors", 0),
        )
        # 신규 주문은 백그라운드 번역 큐에 투입 + Discord 알림
        new_oids = result.get("new_order_ids", [])
        if new_oids:
            asyncio.create_task(_notify_new_orders(new_oids))
        for new_oid in new_oids:
            asyncio.create_task(_translate_safely(new_oid))
    except Exception as e:
        logger.exception("[smartstore-order-poller] 예외")
        _record_job(
            job_id, start, end,
            {"fetched": 0, "inserted": 0, "errors": 1},
            status="error", error=str(e)[:500],
        )


async def _translate_safely(order_id: int) -> None:
    try:
        await translate_order(order_id)
    except Exception:
        logger.exception(
            "[smartstore-order-poller] order %d 번역 태스크 예외", order_id
        )


async def _notify_new_orders(order_ids: list[int]) -> None:
    """신규 주문 N건을 Discord 로 알림. denormalized 컬럼으로 hot.db 만 조회."""
    try:
        from backend.purchase.services.notifier import notify_new_order
        from backend.purchase.database import get_db_hot
        with get_db_hot() as conn:
            placeholders = ",".join("?" * len(order_ids))
            rows = conn.execute(
                f"""SELECT id, channel_order_id, sale_price_krw, quantity,
                           child_asin, asin_cache, product_name_cache
                    FROM orders WHERE id IN ({placeholders})""",
                order_ids,
            ).fetchall()
        for r in rows:
            title = r["product_name_cache"] or "(이름 없음)"
            qty = r["quantity"] or 1
            unit_price = int(r["sale_price_krw"] or 0)
            total = unit_price * qty
            option = r["child_asin"] if r["child_asin"] and r["child_asin"] != r["asin_cache"] else None
            await asyncio.to_thread(
                notify_new_order,
                channel="smartstore",
                product_name=title[:120],
                asin=r["asin_cache"] or "-",
                option=option,
                price_krw=total,
                order_id=str(r["channel_order_id"] or r["id"]),
            )
    except Exception:
        logger.exception("[smartstore-order-poller] 알림 실패 (무시)")


async def run_forever() -> None:
    """lifespan 에서 create_task 로 시작하는 메인 루프."""
    await asyncio.sleep(INITIAL_DELAY_SEC)
    while True:
        try:
            await _poll_once()
        except asyncio.CancelledError:
            logger.info("[smartstore-order-poller] 취소됨")
            raise
        except Exception:
            logger.exception("[smartstore-order-poller] 루프 내 예외 — 계속 진행")
        await asyncio.sleep(POLL_INTERVAL_SEC)
