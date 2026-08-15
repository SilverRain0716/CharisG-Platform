# -*- coding: utf-8 -*-
"""고객 문의 폴러 (2026-07-21 신설).

30분마다 4개 소스 조회 → 신규 문의만 cs_inquiries 에 INSERT →
notified=0 대상만 텔레그램 발송 → notified=1 마킹.

소스:
  - 쿠팡(구/신 각각) 온라인 문의
  - 쿠팡(구/신 각각) 콜센터 문의
  - 쿠팡(구/신 각각) 상품 문의
  - 네이버 상품 Q&A (계정 1개)

pa-api lifespan 에서 run_forever() 로 기동.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from backend.purchase.database import get_db
from backend.purchase.services.coupang_service import coupang_account
from backend.purchase.services import coupang_cs, smartstore_cs
from backend.purchase.services import telegram_service

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 1800   # 30분


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pick_str(d: dict, *keys) -> str:
    """dict 에서 첫 non-empty 값 문자열로 반환."""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return str(v)
    return ""


def _pick_inquiry_id(item: dict, itype: str) -> str:
    return _pick_str(
        item,
        "inquiryId", "onlineInquiryId", "callcenterInquiryId",
        "productInquiryId", "questionId", "id",
    )


def _insert_or_get(conn, channel: str, itype: str, iid: str, **fields) -> Optional[int]:
    """UNIQUE 위반이면 skip (이미 존재). 신규면 rowid 반환."""
    if not iid:
        return None
    cols = ["channel", "inquiry_type", "inquiry_id"] + list(fields.keys())
    vals = [channel, itype, iid] + list(fields.values())
    ph = ",".join("?" * len(vals))
    try:
        cur = conn.execute(
            f"INSERT INTO cs_inquiries ({','.join(cols)}) VALUES ({ph})", vals,
        )
        return cur.lastrowid
    except Exception:
        # UNIQUE 위반 or 스키마 이슈 — 무해
        return None


def _process_items(items: list, channel: str, itype: str,
                    account: Optional[str] = None) -> tuple[int, int]:
    """items 를 cs_inquiries 에 INSERT + 텔레그램 발송.
    반환: (신규건수, 알림성공수)"""
    new_count = 0
    notify_ok = 0
    with get_db() as conn:
        for it in items:
            if not isinstance(it, dict):
                continue
            iid = _pick_inquiry_id(it, itype)
            if not iid:
                continue
            fields = {
                "coupang_account": account,
                "order_id": _pick_str(it, "orderId", "productOrderId",
                                       "customerOrderId") or None,
                "product_id": _pick_str(it, "vendorItemId", "sellerProductId",
                                        "productId", "vendorProductId") or None,
                "customer_name": _pick_str(it, "buyerName", "customerName",
                                            "askUserName", "writerId",
                                            "customerId") or None,
                "title": (_pick_str(it, "title", "subject", "inquiryTitle")
                          or None),
                "content": (_pick_str(it, "content", "inquiryContent",
                                       "question", "text") or None),
                "inquiry_status": _pick_str(it, "answeredType", "status",
                                             "inquiryStatus") or None,
                "created_at": _pick_str(it, "inquiryAt", "createdAt",
                                         "questionRegistrationDate",
                                         "registrationDate") or None,
            }
            rowid = _insert_or_get(conn, channel, itype, iid, **fields)
            if rowid is None:
                continue
            new_count += 1

            # 텔레그램 알림
            try:
                ok = telegram_service.notify_new_inquiry(
                    inquiry_id=iid, channel=channel, inquiry_type=itype,
                    product_name=fields["title"] or None,
                    customer_name=fields["customer_name"],
                    title=fields["title"], content=fields["content"],
                    order_id=fields["order_id"],
                    coupang_account=account,
                )
                if ok:
                    notify_ok += 1
                    conn.execute(
                        "UPDATE cs_inquiries SET notified=1, notified_at=? WHERE id=?",
                        (_now_iso(), rowid),
                    )
            except Exception:
                logger.exception("[cs-poller] 텔레그램 발송 실패 iid=%s", iid)
        conn.commit()
    return new_count, notify_ok


async def _poll_coupang_account(account: str) -> dict:
    """단일 계정 (old|new) 폴링."""
    stats = {"online_new": 0, "callcenter_new": 0, "product_new": 0,
             "online_notify": 0, "callcenter_notify": 0, "product_notify": 0}
    try:
        with coupang_account(account):
            # 3종 순차 조회
            for itype, fn, new_key, notify_key in [
                ("online", coupang_cs.get_online_inquiries,
                 "online_new", "online_notify"),
                ("callcenter", coupang_cs.get_callcenter_inquiries,
                 "callcenter_new", "callcenter_notify"),
                ("product", coupang_cs.get_product_inquiries,
                 "product_new", "product_notify"),
            ]:
                try:
                    items = await asyncio.to_thread(fn)
                except Exception:
                    logger.exception("[cs-poller] 쿠팡/%s/%s 조회 예외",
                                     account, itype)
                    items = []
                if items:
                    n, ok = _process_items(items, "coupang", itype, account)
                    stats[new_key] += n
                    stats[notify_key] += ok
    except Exception:
        logger.exception("[cs-poller] 쿠팡/%s 폴링 예외", account)
    return stats


async def _poll_naver() -> dict:
    stats = {"qna_new": 0, "qna_notify": 0}
    try:
        items = await asyncio.to_thread(smartstore_cs.get_product_questions)
    except Exception:
        logger.exception("[cs-poller] 네이버 문의 조회 예외")
        items = []
    if items:
        n, ok = _process_items(items, "smartstore", "product_qna", None)
        stats["qna_new"] += n
        stats["qna_notify"] += ok
    return stats


async def _poll_once() -> dict:
    total = {}
    # 쿠팡 old + new
    # ★2026-08-12 구계정 영구정지 — 폐쇄 계정은 아예 돌지 않는다.
    #   예외로 배우게 두면 매 주기 로그가 더러워지고 진짜 오류가 묻힌다.
    from backend_shared._config import COUPANG_ALLOW_OLD
    accts = ("old", "new") if COUPANG_ALLOW_OLD else ("new",)
    for acct in accts:
        s = await _poll_coupang_account(acct)
        for k, v in s.items():
            total[f"coupang_{acct}_{k}"] = v
    # 네이버
    ns = await _poll_naver()
    for k, v in ns.items():
        total[f"naver_{k}"] = v
    return total


async def run_forever() -> None:
    """pa-api lifespan 에서 asyncio.create_task 로 기동."""
    logger.info("[cs-inquiry-poller] 기동 (interval=%ds)", POLL_INTERVAL_SEC)
    while True:
        try:
            summary = await _poll_once()
            new_sum = sum(v for k, v in summary.items() if k.endswith("_new"))
            notify_sum = sum(v for k, v in summary.items() if k.endswith("_notify"))
            logger.info(
                "[cs-inquiry-poller] cycle 완료 — 신규=%d 알림=%d %s",
                new_sum, notify_sum, summary,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[cs-inquiry-poller] cycle 예외")
        await asyncio.sleep(POLL_INTERVAL_SEC)
