"""
coupang_settlement_service.py — 쿠팡 정산(settlement) 수집.

공식 OPEN API 2종 (HMAC-SHA256 서명은 coupang_service 재사용):
  - 매출내역  GET /v2/providers/openapi/apis/api/v1/revenue-history
      params: vendorId, recognitionDateFrom/To(yyyy-MM-dd, ≤31일), token, maxPerPage(≤50)
      페이징: 응답 hasNext / nextToken
      → coupang_revenue (주문별 상세)
  - 지급내역  GET /v2/providers/marketplace_openapi/apis/api/v1/settlement-histories
      params: revenueRecognitionYearMonth(yyyy-MM)
      → coupang_settlement (월별 지급 요약)

백필: 2026-01 ~ 현재(KST). 일1회 타이머(scripts/sync_coupang_settlement.py) 또는 POST /api/pa/settlement/sync.
금액 단위 = KRW 정수.
"""
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone

from backend.purchase.database import get_db
from backend.purchase.services import coupang_service
from backend.purchase.services.coupang_service import (
    BASE,
    COUPANG_ACCESS_KEY,
    COUPANG_SECRET_KEY,
    COUPANG_VENDOR_ID,
    _request_with_retry,
    _signature,
)

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
BACKFILL_START_YM = "2026-01"   # 정산 백필 시작월

_REVENUE_PATH = "/v2/providers/openapi/apis/api/v1/revenue-history"
_SETTLEMENT_PATH = "/v2/providers/marketplace_openapi/apis/api/v1/settlement-histories"

SLEEP_API = 0.4   # rate 보호 (정산은 빈도 낮음)

# 첫 응답 raw 1회 로깅 (실제 필드 확정용 — ordersheet 샘플 로깅 패턴)
_REVENUE_SAMPLE_LOGGED = False
_SETTLEMENT_SAMPLE_LOGGED = False


def _gate() -> bool:
    return bool(COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID)


def _to_int(v) -> int | None:
    """금액 문자열/실수 → 정수(원). None/빈값은 None."""
    if v in (None, "", "null"):
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _delivery_amount(v) -> int | None:
    """deliveryFee 가 dict({amount,fee,vat}) 또는 숫자 — amount 우선 추출."""
    if isinstance(v, dict):
        return _to_int(v.get("amount") if v.get("amount") is not None else v.get("fee"))
    return _to_int(v)


# ── 월 범위 유틸 ─────────────────────────────────────────────
def _now_ym() -> str:
    return datetime.now(KST).strftime("%Y-%m")


def _yesterday_kst() -> str:
    """revenue-history 는 '전일까지만' 조회 가능 (당일 포함 시 400). 어제(KST) yyyy-MM-dd."""
    return (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")


def _month_bounds(ym: str) -> tuple[str, str]:
    """'2026-01' → ('2026-01-01', '2026-01-31'). 월 길이 ≤31 이라 revenue 1콜로 커버."""
    y, m = (int(x) for x in ym.split("-"))
    first = date(y, m, 1)
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    last = nxt - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def _iter_months(start_ym: str, end_ym: str):
    y, m = (int(x) for x in start_ym.split("-"))
    ey, em = (int(x) for x in end_ym.split("-"))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m > 12:
            m, y = 1, y + 1


# ── 매출내역 (revenue-history) → coupang_revenue ─────────────
def fetch_revenue_range(date_from: str, date_to: str) -> int:
    """[date_from, date_to] (yyyy-MM-dd, ≤31일) 매출내역 토큰 페이징 수집 → upsert. 반환=row 수."""
    global _REVENUE_SAMPLE_LOGGED
    if not _gate():
        logger.warning("[settlement] 쿠팡 키 미설정 — revenue skip")
        return 0

    token = ""
    total = 0
    page = 0
    while True:
        page += 1
        # 서명/URL 동일 문자열 보장 (token 은 raw 통과 — get_seller_products 패턴과 동일).
        query = (
            f"vendorId={coupang_service._vendor()}"
            f"&recognitionDateFrom={date_from}&recognitionDateTo={date_to}"
            f"&maxPerPage=50&token={token}"
        )
        r = _request_with_retry("GET", BASE + _REVENUE_PATH + "?" + query,
                                headers=_signature("GET", _REVENUE_PATH, query), timeout=20)
        if r is None or r.status_code >= 400:
            logger.warning("[settlement] revenue %s~%s p%d status=%s body=%s",
                           date_from, date_to, page, getattr(r, "status_code", "ERR"),
                           (r.text[:300] if r is not None else ""))
            break
        body = r.json() if r.text else {}
        if not _REVENUE_SAMPLE_LOGGED:
            logger.info("[settlement] revenue raw 샘플(최초1회): %s",
                        json.dumps(body, ensure_ascii=False)[:3000])
            _REVENUE_SAMPLE_LOGGED = True

        data = body.get("data") if isinstance(body, dict) else None
        # data 가 list 이거나 {data:[...], nextToken, hasNext} dict 인 두 형태 모두 대응.
        if isinstance(data, dict):
            rows = data.get("data") or data.get("items") or []
            next_token = data.get("nextToken") or ""
            has_next = bool(data.get("hasNext"))
        else:
            rows = data or []
            next_token = body.get("nextToken") or ""
            has_next = bool(body.get("hasNext"))

        total += _upsert_revenue(rows)
        if not has_next or not next_token:
            break
        token = next_token
        time.sleep(SLEEP_API)
    return total


def _upsert_revenue(rows: list) -> int:
    if not rows:
        return 0
    account = coupang_service.active_account()
    n = 0
    with get_db() as conn:
        for it in rows:
            if not isinstance(it, dict):
                continue
            order_id = str(it.get("orderId") or "")
            if not order_id:
                continue
            # 실측: 금액(salePrice/serviceFee/settlementAmount)은 주문 레벨이 아니라 items[] 안에 있음.
            # 주문 단위 1행으로 items 합산 — service_fee 는 serviceFee + serviceFeeVat(부가세 포함 총수수료).
            items = it.get("items") or []
            if items:
                sale_price = sum(_to_int(x.get("salePrice")) or 0 for x in items)
                service_fee = sum((_to_int(x.get("serviceFee")) or 0) + (_to_int(x.get("serviceFeeVat")) or 0)
                                  for x in items)
                settlement_amount = sum(_to_int(x.get("settlementAmount")) or 0 for x in items)
            else:  # 방어 — 일부 응답이 주문 레벨 금액을 줄 경우
                sale_price = _to_int(it.get("salePrice"))
                service_fee = _to_int(it.get("serviceFee"))
                settlement_amount = _to_int(it.get("settlementAmount"))
            delivery_fee = _delivery_amount(it.get("deliveryFee"))
            # 반품(REFUND)은 양수로 오므로 음수 저장 → 월 매출 SUM 이 net(매출-취소)로 맞음.
            if it.get("saleType") == "REFUND":
                sale_price = -(sale_price or 0)
                service_fee = -(service_fee or 0)
                settlement_amount = -(settlement_amount or 0)
                delivery_fee = -(delivery_fee or 0) if delivery_fee else delivery_fee
            conn.execute(
                """INSERT INTO coupang_revenue
                   (order_id, sale_type, sale_date, recognition_date, settlement_date,
                    sale_price, service_fee, settlement_amount, delivery_fee, items_json,
                    coupang_account, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
                   ON CONFLICT(order_id, sale_type, recognition_date) DO UPDATE SET
                     sale_date=excluded.sale_date,
                     settlement_date=excluded.settlement_date,
                     sale_price=excluded.sale_price,
                     service_fee=excluded.service_fee,
                     settlement_amount=excluded.settlement_amount,
                     delivery_fee=excluded.delivery_fee,
                     items_json=excluded.items_json,
                     coupang_account=excluded.coupang_account,
                     synced_at=datetime('now')""",
                (
                    order_id,
                    it.get("saleType"),
                    it.get("saleDate"),
                    it.get("recognitionDate"),
                    it.get("settlementDate"),
                    sale_price,
                    service_fee,
                    settlement_amount,
                    delivery_fee,
                    json.dumps(it.get("items"), ensure_ascii=False) if it.get("items") is not None else None,
                    account,
                ),
            )
            n += 1
    return n


# ── 지급내역 (settlement-histories) → coupang_settlement ─────
def fetch_settlement_month(ym: str) -> int:
    """인식월(yyyy-MM) 지급내역 수집 → upsert. 반환=row 수."""
    global _SETTLEMENT_SAMPLE_LOGGED
    if not _gate():
        logger.warning("[settlement] 쿠팡 키 미설정 — settlement skip")
        return 0

    query = f"revenueRecognitionYearMonth={ym}"
    r = _request_with_retry("GET", BASE + _SETTLEMENT_PATH + "?" + query,
                            headers=_signature("GET", _SETTLEMENT_PATH, query), timeout=20)
    if r is None or r.status_code >= 400:
        logger.warning("[settlement] settlement %s status=%s body=%s", ym,
                       getattr(r, "status_code", "ERR"), (r.text[:300] if r is not None else ""))
        return 0
    body = r.json() if r.text else {}
    if not _SETTLEMENT_SAMPLE_LOGGED:
        logger.info("[settlement] settlement raw 샘플(최초1회): %s",
                    json.dumps(body, ensure_ascii=False)[:3000])
        _SETTLEMENT_SAMPLE_LOGGED = True

    # 실측: settlement-histories 응답은 최상위가 바로 배열 [{...}] (data 래퍼 없음).
    if isinstance(body, list):
        rows = body
    elif isinstance(body, dict):
        d = body.get("data")
        rows = d if isinstance(d, list) else []
    else:
        rows = []
    return _upsert_settlement(ym, rows)


def _upsert_settlement(ym: str, rows: list) -> int:
    if not rows:
        return 0
    account = coupang_service.active_account()
    n = 0
    with get_db() as conn:
        for s in rows:
            if not isinstance(s, dict):
                continue
            conn.execute(
                """INSERT INTO coupang_settlement
                   (revenue_recognition_ym, settlement_type, settlement_date,
                    recognition_date_from, recognition_date_to,
                    total_sale, service_fee, settlement_target_amount, settlement_amount,
                    last_amount, pending_released_amount, final_amount, status,
                    coupang_account, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
                   ON CONFLICT(revenue_recognition_ym, settlement_type, settlement_date, coupang_account) DO UPDATE SET
                     recognition_date_from=excluded.recognition_date_from,
                     recognition_date_to=excluded.recognition_date_to,
                     total_sale=excluded.total_sale,
                     service_fee=excluded.service_fee,
                     settlement_target_amount=excluded.settlement_target_amount,
                     settlement_amount=excluded.settlement_amount,
                     last_amount=excluded.last_amount,
                     pending_released_amount=excluded.pending_released_amount,
                     final_amount=excluded.final_amount,
                     status=excluded.status,
                     synced_at=datetime('now')""",
                (
                    s.get("revenueRecognitionYearMonth") or ym,
                    s.get("settlementType"),
                    s.get("settlementDate"),
                    s.get("revenueRecognitionDateFrom"),
                    s.get("revenueRecognitionDateTo"),
                    _to_int(s.get("totalSale")),
                    _to_int(s.get("serviceFee")),
                    _to_int(s.get("settlementTargetAmount")),
                    _to_int(s.get("settlementAmount")),
                    _to_int(s.get("lastAmount")),
                    _to_int(s.get("pendingReleasedAmount")),
                    _to_int(s.get("finalAmount")),
                    s.get("status"),
                    account,
                ),
            )
            n += 1
    return n


# ── 통합 sync ────────────────────────────────────────────────
def sync(start_ym: str = BACKFILL_START_YM, end_ym: str | None = None,
         accounts: tuple = ("old", "new")) -> dict:
    """start_ym ~ end_ym(기본=현재월) 의 지급내역 + 매출내역 수집 — 구·신 양 계정.

    백필/일1회 공용. 각 월: settlement-histories 1콜 + revenue-history(월범위 ≤31일) 토큰 페이징.
    각 계정은 coupang_account 컨텍스트로 라우팅되며 행에 coupang_account 태깅됨.
    """
    end_ym = end_ym or _now_ym()
    out = {"start": start_ym, "end": end_ym, "months": 0,
           "settlement_rows": 0, "revenue_rows": 0, "by_account": {}}
    months = list(_iter_months(start_ym, end_ym))
    out["months"] = len(months)
    for account in accounts:
        acc = {"settlement_rows": 0, "revenue_rows": 0}
        with coupang_service.coupang_account(account):
            for ym in months:
                try:
                    acc["settlement_rows"] += fetch_settlement_month(ym)
                    time.sleep(SLEEP_API)
                    d_from, d_to = _month_bounds(ym)
                    # revenue-history 는 '전일까지만' 조회 가능 → 현재월/미래일은 어제로 캡.
                    yday = _yesterday_kst()
                    if d_to > yday:
                        d_to = yday
                    if d_from <= d_to:
                        acc["revenue_rows"] += fetch_revenue_range(d_from, d_to)
                    else:
                        logger.info("[settlement][%s] %s 매출 스킵 — 인식일(전일 이전) 없음", account, ym)
                    time.sleep(SLEEP_API)
                    logger.info("[settlement][%s] %s 완료 (누적 settle=%d revenue=%d)",
                                account, ym, acc["settlement_rows"], acc["revenue_rows"])
                except Exception as e:
                    logger.exception("[settlement][%s] %s 동기화 실패: %s", account, ym, e)
        out["by_account"][account] = acc
        out["settlement_rows"] += acc["settlement_rows"]
        out["revenue_rows"] += acc["revenue_rows"]
    return out
