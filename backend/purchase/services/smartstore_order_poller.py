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

from backend.purchase.database import get_db, get_db_hot
from backend.purchase.services.smartstore_order_sync import sync_orders
from backend.purchase.services.naver_commerce_service import (
    get_changed_product_orders, get_product_order_details,
)
from backend.purchase.services.order_translator import translate_order
from backend.purchase.services.channel_step_mapper import map_channel_to_step
from backend.purchase.services.order_receiver_service import advance_step

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
POLL_INTERVAL_SEC = int(os.environ.get("SMARTSTORE_ORDER_POLL_SEC", "3600"))
INITIAL_DELAY_SEC = 90
# 네이버 lastChangedFrom 은 24h 이내 윈도우만 허용 — 넘으면 일부 응답이 빈 결과로 떨어진다
# (2026-04-27 신규 주문 누락 incident). 안전하게 23시간으로 제한.
POLL_WINDOW_HOURS = 23

# PAYED 외에 advance 트리거 대상 lastChangedType (신규 INSERT 안 함, advance only).
# DELIVERED 는 lastChangedType valid 값이 아니므로 PURCHASE_DECIDED 도달 시 completed.
# 2026-05-10: 네이버가 CANCELED/RETURNED/EXCHANGED/CANCELED_BY_NOPAYMENT 를 deprecate.
# 클레임 흐름이 CLAIM_REQUESTED → CLAIM_HOLDBACK → COLLECT_DONE → CLAIM_REJECTED 사이클로 통합됨.
ADVANCE_TYPES = ["DISPATCHED", "PURCHASE_DECIDED",
                 "CLAIM_REQUESTED", "CLAIM_HOLDBACK", "CLAIM_REJECTED", "COLLECT_DONE"]


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
    """1회 폴링. 예외는 내부에서 흡수해 루프 지속성 보장.

    sync_orders 성공 시 audit log(_record_job) 보다 번역 task / 알림 큐잉을 먼저
    수행한다. _record_job 은 단순 audit 이지만 SQLite 락 경합으로 실패할 수 있고,
    같은 try 블록에 두면 INSERT 된 신규 주문이 번역 큐에 못 들어가는 사고가 난다
    (쿠팡 폴러 2026-05-03 우혜경 주문 67번 사고와 동일 패턴).
    """
    now = datetime.now(tz=KST)
    start_dt = now - timedelta(hours=POLL_WINDOW_HOURS)
    start = _format_kst_iso(start_dt)
    end = _format_kst_iso(now)
    job_id = uuid.uuid4().hex[:12]

    try:
        result = await asyncio.to_thread(
            sync_orders, start, end, "PAYED"
        )
    except Exception as e:
        logger.exception("[smartstore-order-poller] sync_orders 실패")
        try:
            _record_job(
                job_id, start, end,
                {"fetched": 0, "inserted": 0, "errors": 1},
                status="error", error=str(e)[:500],
            )
        except Exception:
            logger.exception("[smartstore-order-poller] _record_job 도 실패 (무시)")
        return

    # 신규 주문 번역/알림 큐잉을 audit log 보다 먼저 실행 — _record_job 실패가 누락을 일으키지 않게
    new_oids = result.get("new_order_ids", [])
    if new_oids:
        asyncio.create_task(_notify_new_orders(new_oids))
    for new_oid in new_oids:
        asyncio.create_task(_translate_safely(new_oid))

    logger.info(
        "[smartstore-order-poller] %s ~ %s — 조회=%d 신규=%d 중복=%d 매핑실패=%d 에러=%d",
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
        logger.exception("[smartstore-order-poller] _record_job 실패 (audit 만 누락, 번역은 진행됨)")

    # ── 추가 lastChangedType 폴링 (INSERT 없이 advance only) ──
    advance_summary: dict[str, int] = {}
    for ct in ADVANCE_TYPES:
        try:
            ids = await asyncio.to_thread(
                get_changed_product_orders, start, end, ct
            ) or []
        except Exception:
            logger.exception("[smartstore-order-poller] get_changed_product_orders type=%s 예외", ct)
            continue
        advanced = 0
        for poid in ids:
            try:
                if _advance_one(str(poid), ct):
                    advanced += 1
            except Exception:
                logger.exception("[smartstore-order-poller] advance %s type=%s 예외", poid, ct)
        advance_summary[ct] = advanced
    if advance_summary:
        logger.info("[smartstore-order-poller] advance %s", advance_summary)

    # ── active 주문 productOrderStatus 직접 조회 ──
    # 네이버는 DELIVERED 를 lastChangedType 로 알려주지 않는다. PURCHASE_DECIDED 가
    # 들어오기 전(보통 8일 보유 기간)에는 lastChanged 폴링만으로 completed 매핑이 안 됨.
    # → DB 의 active(취소/완료 아닌) smartstore 주문 productOrderId 를 매 cycle
    #   productOrders/query 단건 호출해 진짜 status 로 advance.
    try:
        active_count = await asyncio.to_thread(_advance_from_query_active)
        if active_count:
            logger.info("[smartstore-order-poller] active query advance: %d", active_count)
    except Exception:
        logger.exception("[smartstore-order-poller] active query 예외")


def _advance_from_query_active() -> int:
    """active smartstore 주문 productOrderId 일괄 query → productOrderStatus 로 advance.

    lastChanged 폴링이 못 잡는 DELIVERED 케이스 (네이버가 lastChangedType 으로
    DELIVERED 안 보냄) 를 보완. 호출 1회로 active 전체 처리.
    """
    with get_db_hot() as conn:
        rows = conn.execute(
            "SELECT channel_order_id FROM orders "
            "WHERE channel='smartstore' AND current_step NOT IN ('cancelled','completed')"
        ).fetchall()
    poids = [str(r["channel_order_id"]) for r in rows if r["channel_order_id"]]
    if not poids:
        return 0
    details = get_product_order_details(poids) or []
    advanced = 0
    for entry in details:
        po = entry.get("productOrder") or {}
        poid = po.get("productOrderId")
        status = po.get("productOrderStatus")
        if not (poid and status):
            continue
        try:
            if _advance_one(str(poid), str(status)):
                advanced += 1
        except Exception:
            logger.exception("[smartstore-order-poller] active advance 실패 poid=%s", poid)
    return advanced


def _advance_one(channel_order_id: str, channel_status: str) -> bool:
    """DB 매칭 후 forward-only advance.

    cancelled 매핑인 경우 cancel 상세 (status/reason/at) 도 함께 채운다 — 쿠팡
    return_poller 와 대칭. 상세 조회 실패해도 advance 자체는 진행.
    """
    with get_db_hot() as conn:
        row = conn.execute(
            "SELECT id, current_step FROM orders WHERE channel='smartstore' AND channel_order_id=?",
            (channel_order_id,),
        ).fetchone()
    if not row:
        return False
    new_step = map_channel_to_step("smartstore", channel_status, row["current_step"])
    if not new_step:
        return False
    advanced = advance_step(row["id"], new_step, note=f"smartstore:{channel_status}")
    if advanced and new_step == "cancelled":
        try:
            _fill_smartstore_cancel(row["id"], channel_order_id, channel_status)
        except Exception:
            logger.exception(
                "[smartstore-order-poller] cancel 상세 채우기 실패 oid=%s poid=%s",
                row["id"], channel_order_id,
            )
        # 텔레그램 알림 — advance 로 cancelled 신규 진입 시 (idempotent: advance_step 이 이미 전이 여부 판정)
        try:
            _tg_notify_cancel(row["id"])
        except Exception:
            logger.exception("[smartstore-order-poller] 텔레그램 알림 실패 order_id=%s", row["id"])
    return advanced


def _tg_notify_cancel(order_id: int) -> None:
    """스마트스토어 취소/클레임 텔레그램 알림."""
    from backend.purchase.services import telegram_service as _tg
    with get_db_hot() as conn:
        row = conn.execute(
            """SELECT o.channel_order_id, o.sale_price_krw, o.cancel_reason,
                      p.title_ko, p.brand
               FROM orders o LEFT JOIN products p ON p.id=o.product_id
               WHERE o.id=?""",
            (order_id,),
        ).fetchone()
    if not row:
        return
    _tg.notify_order_cancelled(
        order_id=row["channel_order_id"] or order_id,
        channel="smartstore",
        product_name=row["title_ko"],
        brand=row["brand"],
        cancel_reason=row["cancel_reason"],
        sale_price_krw=row["sale_price_krw"],
        cancel_type="CLAIM",
    )


def _fill_smartstore_cancel(order_id: int, product_order_id: str, channel_status: str) -> None:
    """네이버 주문 상세 단건 조회 → orders.cancel_* 컬럼 채움.

    네이버 응답 구조 (2026-05-12 검증):
      entry["cancel"] = {claimId, cancelApprovalDate, cancelCompletedDate,
                          cancelDetailedReason, cancelReason, claimRequestDate,
                          claimStatus, ...}
      entry["currentClaim"]["cancel"] = 동일 내용 (진행중일 때만)
      entry["completedClaims"] = [{claimType, claimStatus, claimRequestReason,
                                    claimRequestDetailContent, ...}, ...]

    cancel/currentClaim.cancel/completedClaims 순서로 시도. productOrder.cancelInfo 는
    네이버 응답에 안 옴 (옛 추정으로 잘못 두었던 경로).
    """
    details = get_product_order_details([product_order_id]) or []
    if not details:
        return
    entry = details[0] or {}
    cancel = (
        entry.get("cancel")
        or (entry.get("currentClaim") or {}).get("cancel")
        or {}
    )
    if not cancel:
        completed = entry.get("completedClaims") or []
        if completed:
            cc = completed[-1] or {}  # 가장 최근 claim
            cancel = {
                "cancelDetailedReason": cc.get("claimRequestDetailContent"),
                "cancelReason": cc.get("claimRequestReason"),
                "cancelCompletedDate": cc.get("claimCompleteOperationDate"),
                "claimRequestDate": cc.get("claimRequestDate"),
                "claimStatus": cc.get("claimStatus"),
            }
    if not cancel:
        return
    po = entry.get("productOrder") or {}
    reason = (cancel.get("cancelDetailedReason") or cancel.get("cancelReason") or "")[:200]
    cancel_at = (
        cancel.get("cancelCompletedDate")
        or cancel.get("cancelApprovalDate")
        or cancel.get("claimRequestDate")
        or ""
    )
    status = cancel.get("claimStatus") or po.get("productOrderStatus") or channel_status or ""
    if not (reason or cancel_at or status):
        return
    with get_db_hot() as conn:
        conn.execute(
            """UPDATE orders SET
               canceled = 1,
               cancel_status = COALESCE(NULLIF(?,''), cancel_status),
               cancel_reason = COALESCE(NULLIF(?,''), cancel_reason),
               cancel_at = COALESCE(cancel_at, NULLIF(?,'')),
               cancel_type = COALESCE(cancel_type, 'CANCEL')
               WHERE id=?""",
            (status[:64], reason, cancel_at, order_id),
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
