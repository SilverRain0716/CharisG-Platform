"""
coupang_order_poller.py — 쿠팡 주문 1시간 폴링.

lifespan에서 asyncio.create_task로 기동. 매 POLL_INTERVAL_SEC 마다
지정한 KST 날짜 범위(오늘 + 어제)로 쿠팡 ordersheet를 읽어 orders 테이블에 upsert.
쿠팡 ordersheet API는 yyyy-MM-dd 단위만 받으므로 시간 증분 대신 날짜로 동작 —
UNIQUE(channel, channel_order_id) 제약으로 중복 호출은 idempotent.
"""
import asyncio
import time
import logging
import uuid
from datetime import datetime, timedelta, timezone

from backend.purchase.database import get_db, get_db_hot
from backend.purchase.services.coupang_service import (
    sync_orders, get_orders as _cp_get_orders, get_orders_v5, coupang_account,
)
from backend.purchase.services.order_translator import translate_order
from backend.purchase.services.channel_step_mapper import map_channel_to_step
from backend.purchase.services.order_receiver_service import advance_step

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
POLL_INTERVAL_SEC = 3600  # 1시간
INITIAL_DELAY_SEC = 60
# 신규 INSERT 폴링 lookback (오늘 포함). 2이면 오늘+어제 — 1시간 주기로 충분.
POLL_DAYS = 2
# ADVANCE 폴링 lookback. 쿠팡 ordersheets 는 createdAtFrom/To = 주문 생성일 기준이라
# 5/3 결제된 주문이 5/9 에 FINAL_DELIVERY 가 돼도 createdAt 이 5/3 이라 좁은 윈도우에선
# 못 잡는다. 결제 후 도착까지 보통 1주~2주 (배대지 경유), 안전하게 14일.
ADVANCE_LOOKBACK_DAYS = 14

# ACCEPT 외에 advance 트리거 대상 status — 신규 INSERT 안 하고 advance_step 만 호출.
# 쿠팡 placedStatus valid: ACCEPT/INSTRUCT/DEPARTURE/DELIVERING/FINAL_DELIVERY/NONE_TRACKING.
# PURCHASE_CONFIRM / CANCEL_DONE 은 ordersheets API 의 valid status 가 아님 (Invalid Status 400).
# 취소/구매확정은 coupang_return_poller 등 별도 폴러가 처리.
ADVANCE_STATUSES = ["INSTRUCT", "DEPARTURE", "DELIVERING", "FINAL_DELIVERY", "NONE_TRACKING"]


def _format_kst_date(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%Y-%m-%d")


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_job(
    job_id: str, start: str, end: str, result: dict, status: str = "done",
    error: str | None = None, account: str = "old",
) -> None:
    acct_label = "신계정" if account == "new" else "구계정"
    msg = (
        f"폴링[{acct_label}] [{start} ~ {end}] — 조회 {result.get('fetched', 0)}, "
        f"신규 {result.get('inserted', 0)}, 중복 {result.get('duplicated', 0)}, "
        f"매핑실패 {result.get('unmapped', 0)}, 에러 {result.get('errors', 0)}"
    )
    if error:
        msg = f"폴링 실패[{acct_label}] [{start} ~ {end}] — {error}"
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
    """1회 폴링 — 구·신 두 계정을 순차 조회.

    각 계정은 독립적으로 폴링(한 계정 실패가 다른 계정을 막지 않음). 신계정 호출은
    coupang_account('new') 컨텍스트로 라우팅되며(contextvar 는 asyncio.to_thread 로
    전파됨), 신계정 신규 주문은 orders.coupang_account='new' 로 태깅된다.
    """
    for account in ("old", "new"):
        try:
            await _poll_account(account)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[coupang-order-poller] %s 계정 폴링 예외 — 다음 계정 진행", account)


def _tag_orders_account(order_ids: list[int], account: str) -> None:
    """방금 INSERT/매칭된 우리 order.id 들의 coupang_account 를 태깅."""
    if not order_ids or account == "old":
        return  # old 는 컬럼 기본값('old') — no-op
    try:
        with get_db_hot() as conn:
            ph = ",".join("?" * len(order_ids))
            conn.execute(
                f"UPDATE orders SET coupang_account=? WHERE channel='coupang' AND id IN ({ph})",
                (account, *order_ids),
            )
    except Exception:
        logger.exception("[coupang-order-poller] 계정 태깅 실패 account=%s", account)


async def _poll_account(account: str) -> None:
    """단일 계정 1회 폴링. 예외는 내부에서 삼켜 루프 지속성 보장.

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
        with coupang_account(account):
            result = await asyncio.to_thread(sync_orders, start, end)
    except Exception as e:
        logger.exception("[coupang-order-poller] sync_orders 실패 account=%s", account)
        try:
            _record_job(
                job_id, start, end, {"fetched": 0, "inserted": 0, "errors": 1},
                status="error", error=str(e)[:500], account=account,
            )
        except Exception:
            logger.exception("[coupang-order-poller] _record_job 도 실패 (무시)")
        return

    # 신규 주문 계정 태깅 — 번역/audit 보다 먼저(DB write 한 번, 락 경합 최소)
    new_ids = result.get("new_order_ids", []) or []
    _tag_orders_account(new_ids, account)

    # 신규 주문 번역 큐잉을 audit log 보다 먼저 실행 — _record_job 실패가 번역 누락을 일으키지 않게
    for new_oid in new_ids:
        asyncio.create_task(_translate_safely(new_oid))

    logger.info(
        "[coupang-order-poller] [%s] %s~%s — 조회=%d 신규=%d 중복=%d 매핑실패=%d 에러=%d",
        account, start, end,
        result.get("fetched", 0),
        result.get("inserted", 0),
        result.get("duplicated", 0),
        result.get("unmapped", 0),
        result.get("errors", 0),
    )

    try:
        _record_job(job_id, start, end, result, status="done", account=account)
    except Exception:
        logger.exception("[coupang-order-poller] _record_job 실패 (audit 만 누락, 번역은 진행됨)")

    # ── 추가 status 폴링 (INSERT 없이 advance only) ──
    # createdAt 기준이라 신규 INSERT 윈도우(2일)와 별개로 14일 lookback. 쿠팡에서
    # 5/3 결제 주문이 5/9 FINAL_DELIVERY 가 돼도 createdAt=5/3 이라 좁은 윈도우엔 안 잡혔던 갭.
    # v5 + nextToken 페이징으로 50건 단일 페이지 한계 회피.
    adv_start = _format_kst_date(now - timedelta(days=ADVANCE_LOOKBACK_DAYS - 1))
    adv_end = end
    advance_summary: dict[str, int] = {}
    for st in ADVANCE_STATUSES:
        sheets: list[dict] = []
        nt = ""
        try:
            while True:
                with coupang_account(account):
                    body = await asyncio.to_thread(
                        get_orders_v5, adv_start, adv_end, st, None, 50, nt,
                    ) or {}
                page = body.get("data") if isinstance(body, dict) else []
                if isinstance(page, list):
                    sheets.extend(page)
                nt = body.get("nextToken") or "" if isinstance(body, dict) else ""
                if not nt:
                    break
        except Exception:
            logger.exception("[coupang-order-poller] get_orders_v5 status=%s account=%s 예외", st, account)
            continue
        advanced = 0
        for sheet in sheets:
            oid = sheet.get("orderId") or sheet.get("orderSheetId")
            if oid is None:
                continue
            try:
                if _advance_one("coupang", str(oid), st):
                    advanced += 1
            except Exception:
                logger.exception("[coupang-order-poller] advance %s status=%s 예외", oid, st)
        advance_summary[st] = advanced
    if advance_summary:
        logger.info("[coupang-order-poller] [%s] advance %s", account, advance_summary)


def _advance_one(channel: str, channel_order_id: str, channel_status: str) -> bool:
    """DB 에서 매칭되는 우리 order 의 current_step 을 forward-only 로 advance."""
    with get_db_hot() as conn:
        row = conn.execute(
            "SELECT id, current_step FROM orders WHERE channel=? AND channel_order_id=?",
            (channel, channel_order_id),
        ).fetchone()
    if not row:
        return False
    new_step = map_channel_to_step(channel, channel_status, row["current_step"])
    if not new_step:
        return False
    return advance_step(row["id"], new_step, note=f"{channel}:{channel_status}")


async def _translate_safely(order_id: int) -> None:
    """번역 실패해도 루프에 영향 없게 예외 삼킴."""
    try:
        await translate_order(order_id)
    except Exception:
        logger.exception("[coupang-order-poller] order %d 번역 태스크 예외", order_id)


_last_translation_sweep = 0.0


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
        # 번역 error 재시도 스윕 (1시간마다) — Gemini 소진 후 회복 시 미수집 자가치유
        try:
            global _last_translation_sweep
            _now = time.time()
            if _now - _last_translation_sweep >= 3600:
                _last_translation_sweep = _now
                from backend.purchase.services.order_translator import retry_failed_translations
                await retry_failed_translations()
        except Exception:
            logger.exception("[coupang-order-poller] 번역 재시도 스윕 예외")
        await asyncio.sleep(POLL_INTERVAL_SEC)
