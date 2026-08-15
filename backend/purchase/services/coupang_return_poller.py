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
from backend.purchase.services.coupang_service import get_return_requests, coupang_account

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
             error or f"CANCEL:{summary.get('cancel', 0)} RU:{summary.get('ru', 0)} UC:{summary.get('uc', 0)} CC:{summary.get('cc', 0)} matched:{summary.get('matched', 0)}"),
        )


def _insert_orphan_cancel_order(conn, item: dict, cancel_type: str, account: str = "old") -> int | None:
    """결제 즉시 자동취소 — order_poller 가 ACCEPT 못 본 사후 캐치.

    returnRequests 응답에서 우리 vendor 주문 검증(returnItems[0].sellerProductId
    가 listings_pa.channel_product_id 에 있음) 후 orders 행 INSERT + order_steps
    cancelled 단계 기록. 고객정보는 응답에 없으므로 NULL. 매출 통계용 placeholder.
    """
    return_items = item.get("returnItems") or []
    if not return_items:
        return None
    first = return_items[0] or {}
    seller_product_id = first.get("sellerProductId")
    if not seller_product_id:
        return None
    # cold.db 의 listings_pa 조회 — get_db_hot 와 다른 커넥션
    with get_db() as cold:
        listing = cold.execute(
            "SELECT id, product_id, sale_krw FROM listings_pa "
            "WHERE channel='coupang' AND channel_product_id=?",
            (str(seller_product_id),),
        ).fetchone()
        product_info = None
        if listing and listing["product_id"]:
            product_info = cold.execute(
                "SELECT title_ko, brand, asin, images_json FROM products WHERE id=?",
                (listing["product_id"],),
            ).fetchone()
    if not listing:
        return None  # 우리 vendor 주문 아님
    order_id = item.get("orderId")
    receipt_id = item.get("receiptId")
    receipt_status = item.get("receiptStatus")
    created_at = item.get("createdAt") or ""
    reason_text = (item.get("reasonCodeText") or item.get("cancelReason") or "")[:200]
    cancel_count_sum = item.get("cancelCountSum") or first.get("cancelCount") or 1
    purchase_count = first.get("purchaseCount") or 1
    sale_krw = listing["sale_krw"] or 0
    title_ko = (product_info["title_ko"] if product_info else None) or first.get("vendorItemPackageName") or first.get("sellerProductName") or "(이름 없음)"
    brand = product_info["brand"] if product_info else None
    asin = product_info["asin"] if product_info else None
    image_url = None
    if product_info and product_info["images_json"]:
        try:
            import json
            imgs = json.loads(product_info["images_json"])
            if isinstance(imgs, list) and imgs:
                first_img = imgs[0]
                image_url = first_img if isinstance(first_img, str) else (first_img.get("url") if isinstance(first_img, dict) else None)
        except Exception:
            pass
    try:
        cur = conn.execute(
            """INSERT INTO orders (
                business_model, channel, channel_order_id, product_id,
                sale_price_krw, quantity, current_step,
                placed_at, ordered_at, paid_at, completed_at,
                product_name_cache, product_image_cache, brand_cache, asin_cache,
                canceled, cancel_count, cancel_receipt_id, cancel_status,
                cancel_reason, cancel_at, cancel_type, coupang_account
            ) VALUES (
                'purchase', 'coupang', ?, ?,
                ?, ?, 'cancelled',
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                1, ?, ?, ?,
                ?, ?, ?, ?
            )""",
            (
                str(order_id), listing["product_id"],
                sale_krw, purchase_count,
                created_at, created_at, created_at, created_at,
                title_ko[:200], image_url, brand, asin,
                cancel_count_sum, int(receipt_id) if receipt_id else None, receipt_status,
                reason_text, created_at, cancel_type, account,
            ),
        )
        new_oid = cur.lastrowid
        conn.execute(
            "INSERT INTO order_steps (order_id, step, label, note) VALUES (?, 'order_received', '주문 접수', ?)",
            (new_oid, "결제 즉시 자동취소 — order_poller 미캐치"),
        )
        conn.execute(
            "INSERT INTO order_steps (order_id, step, label, note) VALUES (?, 'cancelled', '취소', ?)",
            (new_oid, f"coupang:{cancel_type} fallback INSERT"),
        )
        logger.info(
            "[coupang-return-poller] orphan cancel INSERT oid=%s order_id=%s sellerProductId=%s",
            new_oid, order_id, seller_product_id,
        )
        return new_oid
    except Exception:
        # UNIQUE(channel, channel_order_id) 충돌이면 이미 다른 cycle 에서 처리된 것
        logger.exception(
            "[coupang-return-poller] orphan cancel INSERT 실패 order_id=%s", order_id,
        )
        return None


def _apply_return_data(items: list[dict], cancel_type: str, account: str = "old") -> tuple[int, int]:
    """orders 테이블에 cancel 정보 반영. (matched, unmatched).

    매칭 키: orders.channel='coupang' AND channel_order_id=str(orderId).
    account: 사후 orphan-cancel INSERT 시 coupang_account 태깅용.
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
                # 결제 즉시 자동 취소 — order_poller(1h) 가 ACCEPT 상태를 못 본 채로
                # 이미 RETURNS_COMPLETED 가 된 케이스. orders 에 없으니 returnRequests
                # 응답 데이터로 cancelled orders 행을 사후 INSERT (returnItems[0].
                # sellerProductId 가 listings_pa.channel_product_id 에 있을 때만).
                inserted_id = _insert_orphan_cancel_order(conn, it, cancel_type, account)
                if inserted_id:
                    matched += 1
                    _send_cancel_notify(conn, inserted_id, cancel_type, reason_text, account)
                else:
                    unmatched += 1
                continue

            # cancel 확정 판단:
            # - cancelType=CANCEL 면 즉시취소
            # - receiptStatus = RETURNS_COMPLETED → 완전 cancel
            # - receiptStatus = RELEASE_STOP_UNCHECKED / RELEASE_STOP_PROCESSED →
            #   고객이 출고중지 요청한 상태. 셀러 응대 여부와 무관하게 cancel 흐름 진입.
            # - releaseStopStatus = '처리(출고중지)' or '자동처리(이미출고)' → 처리 완료
            is_full_cancel = (
                cancel_type == "CANCEL"
                or receipt_status in (
                    "RETURNS_COMPLETED",
                    "RELEASE_STOP_UNCHECKED",
                    "RELEASE_STOP_PROCESSED",
                )
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
            # 새 취소 전이 감지 시 텔레그램 (이전 canceled=0 → is_full_cancel=1 진입)
            if is_full_cancel and not cur["canceled"]:
                _send_cancel_notify(conn, cur["id"], cancel_type, reason_text, account)
    return matched, unmatched


def _send_cancel_notify(conn, order_id: int, cancel_type: str, reason: str | None,
                         account: str = "old") -> None:
    """취소/반품 텔레그램 알림. 실패는 조용히 삼킴."""
    try:
        from backend.purchase.services import telegram_service as _tg
        row = conn.execute(
            """SELECT o.channel, o.channel_order_id, o.sale_price_krw,
                      p.title_ko, p.brand
               FROM orders o
               LEFT JOIN products p ON p.id=o.product_id
               WHERE o.id=?""",
            (order_id,),
        ).fetchone()
        if not row:
            return
        _tg.notify_order_cancelled(
            order_id=row["channel_order_id"] or order_id,
            channel=row["channel"],
            product_name=row["title_ko"],
            brand=row["brand"],
            cancel_reason=reason,
            sale_price_krw=row["sale_price_krw"],
            cancel_type=cancel_type,
            coupang_account=account,
        )
    except Exception:
        logger.exception("[coupang-return-poller] 텔레그램 알림 실패 order_id=%s", order_id)


async def _poll_once() -> dict:
    """양 계정(구·신) 각각 1회 폴링 후 합산. 한 계정 실패가 다른 계정을 막지 않음."""
    total = {"cancel": 0, "ru": 0, "uc": 0, "cc": 0, "matched": 0, "unmatched": 0, "total": 0}
    for account in ("old", "new"):
        try:
            s = await _poll_account(account)
            for k in total:
                total[k] += s.get(k, 0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[coupang-return-poller] %s 계정 폴링 예외 — 다음 계정 진행", account)
    return total


async def _poll_account(account: str) -> dict:
    """단일 계정 1회 polling — CANCEL + RETURN(RU/UC/CC). coupang_account 컨텍스트로 라우팅."""
    now_kst = datetime.now(KST)
    end_str = _format_kst_date(now_kst)
    start_str = _format_kst_date(now_kst - timedelta(days=LOOKBACK_DAYS))

    summary = {"cancel": 0, "ru": 0, "uc": 0, "cc": 0, "matched": 0, "unmatched": 0, "total": 0}

    # (status, cancel_type, summary_key)
    queries = [
        (None, "CANCEL", "cancel"),   # 결제 즉시취소
        ("RU", "RETURN", "ru"),       # 출고중지요청
        ("UC", "RETURN", "uc"),       # 반품접수
        ("CC", "RETURN", "cc"),       # 반품완료
    ]
    for status, cancel_type, key in queries:
        try:
            with coupang_account(account):
                body = await asyncio.to_thread(
                    get_return_requests,
                    start_str, end_str, status=status, cancel_type=cancel_type,
                    search_type=None, max_per_page=50,
                )
            if body:
                data = body.get("data") or []
                summary[key] = len(data)
                m, um = _apply_return_data(data, cancel_type, account)
                summary["matched"] += m
                summary["unmatched"] += um
                summary["total"] += len(data)
        except Exception as e:
            logger.exception(
                "[coupang-return-poller] %s/%s polling 예외 account=%s: %s",
                cancel_type, status, account, e,
            )

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
                f"RU={summary['ru']} UC={summary['uc']} CC={summary['cc']} "
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
