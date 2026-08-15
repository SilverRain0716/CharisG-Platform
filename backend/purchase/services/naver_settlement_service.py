"""
naver_settlement_service.py — 네이버(스마트스토어) 정산 수집.

공식 커머스 API 2종 (OAuth2 Bearer = naver_commerce_service 토큰 재사용):
  - 일별  GET /v1/pay-settle/settle/daily  (startDate,endDate,pageNumber,pageSize≤1000)
          → {elements:[...], pagination}. settleAmount=정산금액, settleExpectDate=정산예정일.
          → naver_settlement (월별 집계: settle_expect_date 기준)
  - 건별  GET /v1/pay-settle/settle/case   (searchDate 단일일, periodType, pageNumber, pageSize≤1000)
          → per-order. settleExpectAmount=정산예정금액. → naver_revenue

백필: 2026-01~현재. 건별은 '일별에서 정산이 발생한 settleExpectDate' 만 골라 조회(낭비 방지).
금액 = KRW 정수. 첫 응답 raw 1회 로깅(필드 확정용).
"""
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone

from backend.purchase.database import get_db
from backend.purchase.services.naver_commerce_service import (
    BASE,
    _get_token,
    _request_with_retry,
)

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
BACKFILL_START_YM = "2026-01"

_DAILY_PATH = "/v1/pay-settle/settle/daily"
_CASE_PATH = "/v1/pay-settle/settle/case"
# 건별 조회 기간 기준 = 정산 예정일 (일별의 settleExpectDate 와 정렬 맞춤)
_CASE_PERIOD_TYPE = "SETTLE_CASEBYCASE_SETTLE_SCHEDULE_DATE"

SLEEP_API = 0.3
_PAGE_SIZE = 1000

_DAILY_SAMPLE_LOGGED = False
_CASE_SAMPLE_LOGGED = False


def _to_int(v) -> int | None:
    if v in (None, "", "null"):
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _now_ym() -> str:
    return datetime.now(KST).strftime("%Y-%m")


def _yesterday_kst() -> str:
    return (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")


def _month_bounds(ym: str) -> tuple[str, str]:
    y, m = (int(x) for x in ym.split("-"))
    first = date(y, m, 1)
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def _iter_months(start_ym: str, end_ym: str):
    y, m = (int(x) for x in start_ym.split("-"))
    ey, em = (int(x) for x in end_ym.split("-"))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m > 12:
            m, y = 1, y + 1


def _get(path: str, params: dict) -> dict | None:
    token = _get_token()
    if not token:
        logger.warning("[naver-settle] 네이버 토큰 미발급 — skip")
        return None
    try:
        r = _request_with_retry(
            "GET", BASE + path,
            headers={"Authorization": f"Bearer {token}"},
            params=params, timeout=20,
        )
    except Exception as e:
        logger.error("[naver-settle] %s 예외: %s", path, e)
        return None
    if r is None or r.status_code >= 400:
        logger.warning("[naver-settle] %s status=%s params=%s body=%s", path,
                       getattr(r, "status_code", "ERR"), params,
                       (r.text[:300] if r is not None else ""))
        return None
    return r.json() if r.text else {}


# ── 일별 정산 (settle/daily) → naver_settlement ───────────────
def fetch_daily_range(start_date: str, end_date: str) -> set[str]:
    """[start_date,end_date] 일별 정산 페이징 수집 → upsert. 반환=정산예정일(settle_expect_date) 집합."""
    global _DAILY_SAMPLE_LOGGED
    expect_dates: set[str] = set()
    page = 1
    while True:
        body = _get(_DAILY_PATH, {
            "startDate": start_date, "endDate": end_date,
            "pageNumber": page, "pageSize": _PAGE_SIZE,
        })
        if body is None:
            break
        if not _DAILY_SAMPLE_LOGGED:
            logger.info("[naver-settle] daily raw 샘플(최초1회): %s",
                        json.dumps(body, ensure_ascii=False)[:3000])
            _DAILY_SAMPLE_LOGGED = True
        elements = body.get("elements") or []
        for d in _upsert_daily(elements):
            expect_dates.add(d)
        pg = body.get("pagination") or {}
        total_pages = pg.get("totalPages") or 1
        if page >= total_pages or not elements:
            break
        page += 1
        time.sleep(SLEEP_API)
    return expect_dates


def _upsert_daily(rows: list) -> list[str]:
    if not rows:
        return []
    seen: list[str] = []
    with get_db() as conn:
        for s in rows:
            if not isinstance(s, dict):
                continue
            exp = s.get("settleExpectDate")
            if exp:
                seen.append(exp)
            conn.execute(
                """INSERT INTO naver_settlement
                   (settle_expect_date, settle_basis_start_date, settle_basis_end_date,
                    settle_complete_date, settle_amount, pay_settle_amount, commission_settle_amount,
                    benefit_settle_amount, deduction_restore_amount, pay_holdback_amount,
                    normal_settle_amount, quick_settle_amount, preferential_commission,
                    settle_method_type, bank_type, depositor_name, account_no,
                    merchant_id, merchant_name, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
                   ON CONFLICT(settle_expect_date, settle_basis_start_date, settle_basis_end_date,
                               settle_method_type, bank_type) DO UPDATE SET
                     settle_complete_date=excluded.settle_complete_date,
                     settle_amount=excluded.settle_amount,
                     pay_settle_amount=excluded.pay_settle_amount,
                     commission_settle_amount=excluded.commission_settle_amount,
                     benefit_settle_amount=excluded.benefit_settle_amount,
                     deduction_restore_amount=excluded.deduction_restore_amount,
                     pay_holdback_amount=excluded.pay_holdback_amount,
                     normal_settle_amount=excluded.normal_settle_amount,
                     quick_settle_amount=excluded.quick_settle_amount,
                     preferential_commission=excluded.preferential_commission,
                     depositor_name=excluded.depositor_name,
                     account_no=excluded.account_no,
                     merchant_name=excluded.merchant_name,
                     synced_at=datetime('now')""",
                (
                    s.get("settleExpectDate"), s.get("settleBasisStartDate"), s.get("settleBasisEndDate"),
                    s.get("settleCompleteDate"),
                    _to_int(s.get("settleAmount")), _to_int(s.get("paySettleAmount")),
                    _to_int(s.get("commissionSettleAmount")), _to_int(s.get("benefitSettleAmount")),
                    _to_int(s.get("deductionRestoreSettleAmount")), _to_int(s.get("payHoldbackAmount")),
                    _to_int(s.get("normalSettleAmount")), _to_int(s.get("quickSettleAmount")),
                    _to_int(s.get("preferentialCommissionAmount")),
                    s.get("settleMethodType"), s.get("bankType"), s.get("depositorName"),
                    s.get("accountNo"), s.get("merchantId"), s.get("merchantName"),
                ),
            )
    return seen


# ── 건별 정산 (settle/case) → naver_revenue ───────────────────
def fetch_case_day(search_date: str) -> int:
    """단일일(정산예정일=search_date) 건별 정산 페이징 수집 → upsert. 반환=row 수."""
    global _CASE_SAMPLE_LOGGED
    total = 0
    page = 1
    while True:
        body = _get(_CASE_PATH, {
            "searchDate": search_date, "periodType": _CASE_PERIOD_TYPE,
            "pageNumber": page, "pageSize": _PAGE_SIZE,
        })
        if body is None:
            break
        if not _CASE_SAMPLE_LOGGED:
            logger.info("[naver-settle] case raw 샘플(최초1회): %s",
                        json.dumps(body, ensure_ascii=False)[:3000])
            _CASE_SAMPLE_LOGGED = True
        elements = body.get("elements") or []
        total += _upsert_case(elements)
        pg = body.get("pagination") or {}
        total_pages = pg.get("totalPages") or 1
        if page >= total_pages or not elements:
            break
        page += 1
        time.sleep(SLEEP_API)
    return total


def _upsert_case(rows: list) -> int:
    if not rows:
        return 0
    n = 0
    with get_db() as conn:
        for it in rows:
            if not isinstance(it, dict):
                continue
            pid = str(it.get("productOrderId") or it.get("orderId") or "")
            if not pid:
                continue
            # 총 수수료 = npay관리 + 매출연동 + 무이자할부 (음수=차감일 수 있음 → 저장 그대로, 표시는 라우터에서 abs)
            commission = sum(
                _to_int(it.get(k)) or 0
                for k in ("totalPayCommissionAmount", "sellingInterlockCommissionAmount",
                          "freeInstallmentCommissionAmount")
            )
            conn.execute(
                """INSERT INTO naver_revenue
                   (product_order_id, order_id, product_order_type, settle_type,
                    settle_basis_date, settle_expect_date, settle_complete_date, pay_date,
                    product_id, product_name, purchaser_name,
                    pay_settle_amount, commission_amount, benefit_settle_amount,
                    settle_expect_amount, merchant_id, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
                   ON CONFLICT(product_order_id, settle_type, settle_basis_date) DO UPDATE SET
                     settle_expect_date=excluded.settle_expect_date,
                     settle_complete_date=excluded.settle_complete_date,
                     pay_date=excluded.pay_date,
                     product_name=excluded.product_name,
                     pay_settle_amount=excluded.pay_settle_amount,
                     commission_amount=excluded.commission_amount,
                     benefit_settle_amount=excluded.benefit_settle_amount,
                     settle_expect_amount=excluded.settle_expect_amount,
                     synced_at=datetime('now')""",
                (
                    pid, str(it.get("orderId") or "") or None,
                    it.get("productOrderType"), it.get("settleType"),
                    it.get("settleBasisDate"), it.get("settleExpectDate"),
                    it.get("settleCompleteDate"), it.get("payDate"),
                    it.get("productId"), it.get("productName"), it.get("purchaserName"),
                    _to_int(it.get("paySettleAmount")), commission,
                    _to_int(it.get("benefitSettleAmount")), _to_int(it.get("settleExpectAmount")),
                    it.get("merchantId"),
                ),
            )
            n += 1
    return n


# ── 통합 sync ────────────────────────────────────────────────
def sync(start_ym: str = BACKFILL_START_YM, end_ym: str | None = None) -> dict:
    """start_ym~end_ym(기본=현재월). 월별 일별정산 수집 → 정산발생 예정일만 건별 수집."""
    end_ym = end_ym or _now_ym()
    out = {"start": start_ym, "end": end_ym, "months": 0,
           "case_dates": 0, "revenue_rows": 0}
    yday = _yesterday_kst()

    # 1단계: 월별 일별정산 수집 → 정산 발생 예정일 전체 집합 (중복 제거)
    all_expect: set[str] = set()
    for ym in _iter_months(start_ym, end_ym):
        out["months"] += 1
        try:
            d_from, d_to = _month_bounds(ym)
            if d_to > yday:
                d_to = yday
            if d_from > d_to:
                continue
            all_expect |= fetch_daily_range(d_from, d_to)
            time.sleep(SLEEP_API)
        except Exception as e:
            logger.exception("[naver-settle] daily %s 실패: %s", ym, e)

    # 2단계: 정산 발생한 예정일만 건별 조회 (전일까지, 유니크 1회씩)
    for ed in sorted(d for d in all_expect if d and d <= yday):
        out["case_dates"] += 1
        try:
            out["revenue_rows"] += fetch_case_day(ed)
        except Exception as e:
            logger.exception("[naver-settle] case %s 실패: %s", ed, e)
        time.sleep(SLEEP_API)
    logger.info("[naver-settle] sync 완료: 월=%d 예정일=%d revenue=%d",
                out["months"], out["case_dates"], out["revenue_rows"])
    return out
