"""PA Settlement — 채널별 정산: 월별 지급 요약 + 주문/건별 상세.

채널: ?channel=coupang(기본) | naver  — 응답 shape 은 채널 무관 통일(프론트 재사용).

- GET  /api/pa/settlement/summary?channel=&date_from=&date_to=   월별 집계(전월대비 증감)
- GET  /api/pa/settlement/revenue?channel=&date_from=&date_to=&sale_type=&before_id=  상세(keyset)
- GET  /api/pa/settlement/status?channel=                        마지막 동기화/건수
- POST /api/pa/settlement/sync   body{channel?,start_ym?,end_ym?}  백그라운드 수집

수집: coupang_settlement_service(revenue-history+settlement-histories) / naver_settlement_service(settle daily+case).
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends

from backend.purchase.auth import current_user
from backend.purchase.database import get_db, get_db_hot
from backend.purchase.services import coupang_settlement_service as csvc
from backend.purchase.services import naver_settlement_service as nsvc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pa/settlement", tags=["pa-settlement"])

# 마지막 sync 결과 (채널별, 프로세스 메모리)
_LAST_SYNC: dict = {
    "coupang": {"running": False, "result": None, "error": None},
    "naver":   {"running": False, "result": None, "error": None},
}


def _mom(months: list) -> list:
    """전월대비 증감(final_amount 기준) 부여. months 는 ym ASC 정렬 가정."""
    prev = None
    for d in months:
        cur = d.get("final_amount") or 0
        if prev is None:
            d["mom_delta"] = None; d["mom_pct"] = None
        else:
            d["mom_delta"] = cur - prev
            d["mom_pct"] = round((cur - prev) / prev * 100, 1) if prev else None
        prev = cur
    return months


# ══════════════════════ SUMMARY ══════════════════════
@router.get("/summary")
def settlement_summary(
    channel: str = "coupang",
    date_from: str | None = None,  # 'YYYY-MM'
    date_to: str | None = None,
    account: str | None = None,    # 쿠팡 계정: 'old'|'new' (None=전체)
    user: dict = Depends(current_user),
):
    return _naver_summary(date_from, date_to) if channel == "naver" \
        else _coupang_summary(date_from, date_to, account)


def _coupang_summary(date_from, date_to, account=None):
    """지급액=coupang_settlement SUM(final_amount)(distinct 지급, 합산안전).
    매출/수수료/정산액=coupang_revenue(주문별, 이중계산 없음). 인식월 병합."""
    acct = account if account in ("old", "new") else None
    s_where, s_params = [], []
    if date_from:
        s_where.append("revenue_recognition_ym >= ?"); s_params.append(date_from)
    if date_to:
        s_where.append("revenue_recognition_ym <= ?"); s_params.append(date_to)
    if acct:
        s_where.append("coupang_account = ?"); s_params.append(acct)
    s_wsql = (" WHERE " + " AND ".join(s_where)) if s_where else ""

    r_where = ["recognition_date IS NOT NULL"]; r_params = []
    if date_from:
        r_where.append("substr(recognition_date,1,7) >= ?"); r_params.append(date_from)
    if date_to:
        r_where.append("substr(recognition_date,1,7) <= ?"); r_params.append(date_to)
    if acct:
        r_where.append("coupang_account = ?"); r_params.append(acct)
    r_wsql = " WHERE " + " AND ".join(r_where)

    with get_db() as conn:
        set_rows = conn.execute(
            f"""SELECT revenue_recognition_ym AS ym, SUM(final_amount) AS final_amount,
                       SUM(CASE WHEN status='DONE' THEN final_amount ELSE 0 END) AS final_paid,
                       SUM(CASE WHEN status IS NULL OR status<>'DONE' THEN final_amount ELSE 0 END) AS final_scheduled,
                       COUNT(*) AS payout_count, MIN(status) AS any_status, MAX(synced_at) AS synced_at
                FROM coupang_settlement{s_wsql} GROUP BY revenue_recognition_ym""", s_params).fetchall()
        rev_rows = conn.execute(
            f"""SELECT substr(recognition_date,1,7) AS ym, SUM(sale_price) AS total_sale,
                       SUM(service_fee) AS service_fee, SUM(settlement_amount) AS settlement_amount,
                       COUNT(*) AS order_count
                FROM coupang_revenue{r_wsql} GROUP BY substr(recognition_date,1,7)""", r_params).fetchall()
        detail = conn.execute(
            f"""SELECT revenue_recognition_ym AS ym, settlement_type, settlement_date,
                       final_amount, settlement_amount, total_sale, service_fee, status
                FROM coupang_settlement{s_wsql}
                ORDER BY revenue_recognition_ym ASC, settlement_date ASC""", s_params).fetchall()

    merged: dict[str, dict] = {}
    for r in set_rows:
        d = dict(r)
        merged[d["ym"]] = {**d, "total_sale": 0, "service_fee": 0, "settlement_amount": 0, "order_count": 0}
    for r in rev_rows:
        d = dict(r)
        m = merged.setdefault(d["ym"], {"ym": d["ym"], "final_amount": 0, "final_paid": 0,
                                        "final_scheduled": 0, "payout_count": 0,
                                        "any_status": None, "synced_at": None})
        m.update(total_sale=d["total_sale"], service_fee=d["service_fee"],
                 settlement_amount=d["settlement_amount"], order_count=d["order_count"])

    months = _mom([merged[ym] for ym in sorted(merged)])
    breakdown: dict[str, list] = {}
    for r in detail:
        breakdown.setdefault(r["ym"], []).append(dict(r))

    # 실시간 주문 매출 — orders 테이블 기준 gross(비취소). 정산 인식 전 최근분 포함이라
    # 쿠팡 셀러센터 누적매출과 대조됨. coupang_revenue(인식분)는 정산 지연으로 더 작음.
    o_where = ["channel='coupang'", "current_step != 'cancelled'"]
    o_params: list = []
    if date_from:
        o_where.append("substr(ordered_at,1,7) >= ?"); o_params.append(date_from)
    if date_to:
        o_where.append("substr(ordered_at,1,7) <= ?"); o_params.append(date_to)
    if acct:
        o_where.append("coupang_account = ?"); o_params.append(acct)
    o_wsql = " WHERE " + " AND ".join(o_where)
    with get_db_hot() as hconn:
        o_row = hconn.execute(
            f"SELECT COALESCE(SUM(sale_price_krw),0) AS rev, COUNT(*) AS cnt FROM orders{o_wsql}",
            o_params).fetchone()
        om_rows = hconn.execute(
            f"""SELECT substr(ordered_at,1,7) AS ym, COALESCE(SUM(sale_price_krw),0) AS rev,
                       COUNT(*) AS cnt FROM orders{o_wsql} GROUP BY substr(ordered_at,1,7)""",
            o_params).fetchall()
    realtime_by_ym = {r["ym"]: {"order_revenue": r["rev"], "order_count": r["cnt"]} for r in om_rows}
    for m in months:
        rt = realtime_by_ym.get(m["ym"])
        m["order_revenue_realtime"] = (rt or {}).get("order_revenue", 0)
        m["order_count_realtime"] = (rt or {}).get("order_count", 0)

    return {"channel": "coupang", "months": months, "breakdown": breakdown,
            "total_final_amount": sum((m.get("final_amount") or 0) for m in months),
            "total_paid": sum((m.get("final_paid") or 0) for m in months),
            "total_scheduled": sum((m.get("final_scheduled") or 0) for m in months),
            "realtime_order_revenue": o_row["rev"] if o_row else 0,
            "realtime_order_count": o_row["cnt"] if o_row else 0,
            "month_count": len(months)}


def _naver_summary(date_from, date_to):
    """지급액=naver_settlement SUM(settle_amount)(일별 distinct). 매출=pay_settle_amount,
    수수료=ABS(commission). 정산예정일(settle_expect_date) 의 월로 집계. 주문수=naver_revenue."""
    s_where = ["settle_expect_date IS NOT NULL"]; s_params = []
    if date_from:
        s_where.append("substr(settle_expect_date,1,7) >= ?"); s_params.append(date_from)
    if date_to:
        s_where.append("substr(settle_expect_date,1,7) <= ?"); s_params.append(date_to)
    s_wsql = " WHERE " + " AND ".join(s_where)

    with get_db() as conn:
        set_rows = conn.execute(
            f"""SELECT substr(settle_expect_date,1,7) AS ym,
                       SUM(settle_amount)                AS final_amount,
                       SUM(CASE WHEN settle_complete_date IS NOT NULL THEN settle_amount ELSE 0 END) AS final_paid,
                       SUM(CASE WHEN settle_complete_date IS NULL THEN settle_amount ELSE 0 END)     AS final_scheduled,
                       SUM(pay_settle_amount)            AS total_sale,
                       ABS(SUM(commission_settle_amount)) AS service_fee,
                       SUM(settle_amount)                AS settlement_amount,
                       COUNT(*)                          AS payout_count,
                       MAX(synced_at)                    AS synced_at
                FROM naver_settlement{s_wsql} GROUP BY substr(settle_expect_date,1,7)""", s_params).fetchall()
        rev_rows = conn.execute(
            f"""SELECT substr(settle_expect_date,1,7) AS ym, COUNT(*) AS order_count
                FROM naver_revenue WHERE settle_expect_date IS NOT NULL
                {'AND substr(settle_expect_date,1,7) >= ?' if date_from else ''}
                {'AND substr(settle_expect_date,1,7) <= ?' if date_to else ''}
                GROUP BY substr(settle_expect_date,1,7)""",
            [x for x in (date_from, date_to) if x]).fetchall()
        detail = conn.execute(
            f"""SELECT substr(settle_expect_date,1,7) AS ym, settle_method_type AS settlement_type,
                       settle_expect_date AS settlement_date, settle_amount AS final_amount,
                       settle_amount AS settlement_amount, pay_settle_amount AS total_sale,
                       ABS(commission_settle_amount) AS service_fee, bank_type AS status
                FROM naver_settlement{s_wsql}
                ORDER BY settle_expect_date ASC""", s_params).fetchall()

    merged: dict[str, dict] = {}
    for r in set_rows:
        d = dict(r); d["any_status"] = None; d["order_count"] = 0
        merged[d["ym"]] = d
    for r in rev_rows:
        merged.setdefault(r["ym"], {"ym": r["ym"], "final_amount": 0, "final_paid": 0,
                                    "final_scheduled": 0})["order_count"] = r["order_count"]

    months = _mom([merged[ym] for ym in sorted(merged)])
    breakdown: dict[str, list] = {}
    for r in detail:
        breakdown.setdefault(r["ym"], []).append(dict(r))
    return {"channel": "naver", "months": months, "breakdown": breakdown,
            "total_final_amount": sum((m.get("final_amount") or 0) for m in months),
            "total_paid": sum((m.get("final_paid") or 0) for m in months),
            "total_scheduled": sum((m.get("final_scheduled") or 0) for m in months),
            "month_count": len(months)}


# ══════════════════════ REVENUE (상세) ══════════════════════
@router.get("/revenue")
def settlement_revenue(
    channel: str = "coupang",
    date_from: str | None = None,  # 'YYYY-MM-dd'
    date_to: str | None = None,
    sale_type: str | None = None,
    before_id: int | None = None,
    limit: int = 50,
    account: str | None = None,    # 쿠팡 계정: 'old'|'new'
    user: dict = Depends(current_user),
):
    limit = max(1, min(limit, 200))
    if channel == "naver":
        return _naver_revenue(date_from, date_to, sale_type, before_id, limit)
    return _coupang_revenue(date_from, date_to, sale_type, before_id, limit, account)


def _coupang_revenue(date_from, date_to, sale_type, before_id, limit, account=None):
    where, params = [], []
    if date_from:
        where.append("recognition_date >= ?"); params.append(date_from)
    if date_to:
        where.append("recognition_date <= ?"); params.append(date_to)
    if sale_type:
        where.append("sale_type = ?"); params.append(sale_type)
    if account in ("old", "new"):
        where.append("coupang_account = ?"); params.append(account)
    if before_id and before_id > 0:
        where.append("id < ?"); params.append(before_id)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT id, order_id, sale_type, recognition_date, settlement_date,
                       sale_price, service_fee, settlement_amount, NULL AS product_name
                FROM coupang_revenue{wsql} ORDER BY id DESC LIMIT ?""",
            params + [limit + 1]).fetchall()
    return _page(rows, limit)


def _naver_revenue(date_from, date_to, sale_type, before_id, limit):
    """건별 정산 상세 → 통합 item shape (settle_expect_date 를 settlement_date 로)."""
    where, params = [], []
    if date_from:
        where.append("settle_expect_date >= ?"); params.append(date_from)
    if date_to:
        where.append("settle_expect_date <= ?"); params.append(date_to)
    if sale_type:
        where.append("settle_type = ?"); params.append(sale_type)
    if before_id and before_id > 0:
        where.append("id < ?"); params.append(before_id)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT id, order_id, product_order_type AS sale_type, settle_basis_date AS recognition_date,
                       settle_expect_date AS settlement_date, pay_settle_amount AS sale_price,
                       ABS(commission_amount) AS service_fee, settle_expect_amount AS settlement_amount,
                       product_name
                FROM naver_revenue{wsql} ORDER BY id DESC LIMIT ?""",
            params + [limit + 1]).fetchall()
    return _page(rows, limit)


def _page(rows, limit):
    items = [dict(r) for r in rows[:limit]]
    has_more = len(rows) > limit
    return {"items": items, "has_more": has_more,
            "next_cursor": (items[-1]["id"] if (items and has_more) else None)}


# ══════════════════════ STATUS ══════════════════════
@router.get("/status")
def settlement_status(channel: str = "coupang", user: dict = Depends(current_user)):
    st = _LAST_SYNC.get(channel, _LAST_SYNC["coupang"])
    if channel == "naver":
        set_tbl, rev_tbl, rev_date = "naver_settlement", "naver_revenue", "settle_expect_date"
    else:
        set_tbl, rev_tbl, rev_date = "coupang_settlement", "coupang_revenue", "recognition_date"
    with get_db() as conn:
        s = conn.execute(f"SELECT COUNT(*) c, MAX(synced_at) last FROM {set_tbl}").fetchone()
        rev = conn.execute(
            f"SELECT COUNT(*) c, MAX(synced_at) last, MIN({rev_date}) min_d, MAX({rev_date}) max_d "
            f"FROM {rev_tbl}").fetchone()
    return {
        "channel": channel,
        "settlement_rows": s["c"], "settlement_last_synced": s["last"],
        "revenue_rows": rev["c"], "revenue_last_synced": rev["last"],
        "revenue_date_min": rev["min_d"], "revenue_date_max": rev["max_d"],
        "running": st["running"], "last_result": st["result"], "last_error": st["error"],
    }


# ══════════════════════ SYNC ══════════════════════
def _run_sync(channel: str, start_ym, end_ym) -> None:
    st = _LAST_SYNC[channel]
    st.update(running=True, error=None)
    svc = nsvc if channel == "naver" else csvc
    try:
        st["result"] = svc.sync(start_ym or svc.BACKFILL_START_YM, end_ym)
        logger.info("[settlement:%s] sync 완료: %s", channel, st["result"])
    except Exception as e:
        st["error"] = str(e)
        logger.exception("[settlement:%s] sync 실패: %s", channel, e)
    finally:
        st["running"] = False


@router.post("/sync")
def settlement_sync(
    background: BackgroundTasks,
    body: dict | None = None,
    user: dict = Depends(current_user),
):
    """백그라운드 수집. body: {channel?, start_ym?, end_ym?}. nginx 120s 회피 비동기."""
    body = body or {}
    channel = body.get("channel", "coupang")
    if channel not in _LAST_SYNC:
        return {"started": False, "message": f"알 수 없는 채널: {channel}"}
    if _LAST_SYNC[channel]["running"]:
        return {"started": False, "message": "이미 동기화 진행 중"}
    svc = nsvc if channel == "naver" else csvc
    start_ym = body.get("start_ym")
    background.add_task(_run_sync, channel, start_ym, body.get("end_ym"))
    return {"started": True, "channel": channel,
            "start_ym": start_ym or svc.BACKFILL_START_YM, "end_ym": body.get("end_ym")}
