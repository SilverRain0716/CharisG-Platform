# -*- coding: utf-8 -*-
"""채널 공용 리스팅 목록. 2026-08-15 사장 지시 (P2-1)

★왜 공용인가 — 쿠팡·네이버 화면이 각자 진화해 서로 다른 코드가 됐다. 그런데
  목록이 보여줘야 하는 것(상품명·가격·상태·옵션·마진)은 채널이 달라도 같다.
  11번가·옥션 화면을 또 따로 만들면 네 벌이 되고, 오늘 같은 어긋남이 네 배로 늘어난다.

★이 API 가 채널을 가리지 않는 이유 — listings_pa 가 이미 채널 공용 테이블이다.
  채널별로 다른 것은 '상태를 뭐라 부르는가'뿐이고, 그건 channel_status 에 원문으로 있다.

★반드시 같이 내보내는 것 —
      channel_status      채널 원문. 우리 판정(status)과 나란히 둔다.
      channel_checked_at  언제 확인했나. 오래된 값을 현재로 착각하지 않기 위해.
      option_total/with_id 옵션 회수 상태 — 주문이 왔을 때 자식을 특정할 수 있는가
  실측(2026-08-15): 네이버 18건이 우리 DB 로는 paused 인데 채널에선 전부 SALE 이었다.
  둘을 나란히 안 보여주면 이런 걸 영영 못 본다.
"""
from fastapi import APIRouter, Depends, Query

from backend.purchase.auth import current_user
from backend.purchase.database import get_db
from backend.purchase.listing_status import DEAD, SELLING

router = APIRouter(prefix="/api/pa", tags=["pa-listings"])


@router.get("/listings")
def list_listings(
    channel: str | None = Query(None, description="coupang·smartstore·elevenst·auction·gmarket"),
    account: str | None = Query(None, description="old | new"),
    state: str = Query("live", description="live=살아있는 것 · selling=채널에서 판매중 · all"),
    q: str | None = Query(None, description="상품명·ASIN·상품번호 부분일치"),
    limit: int = Query(100, le=500),
    offset: int = 0,
    user: dict = Depends(current_user),
):
    where, args = ["l.channel_product_id IS NOT NULL"], []
    if channel:
        where.append("l.channel = ?")
        args.append(channel)
    if account:
        where.append("COALESCE(l.acct_key, l.coupang_account, l.naver_account) = ?")
        args.append(account)
    if state == "live":
        where.append("l.status NOT IN (%s)" % ",".join("?" * len(DEAD)))
        args += list(DEAD)
    elif state == "selling":
        where.append("l.channel_status IN (%s)" % ",".join("?" * len(SELLING)))
        args += list(SELLING)
    if q:
        where.append("(p.title_ko LIKE ? OR p.asin LIKE ? OR l.channel_product_id LIKE ?)")
        args += ["%%%s%%" % q] * 3

    sql = """
        SELECT l.id, l.channel, COALESCE(l.acct_key, l.coupang_account, l.naver_account) account,
               l.channel_product_id, l.status, l.channel_status, l.channel_checked_at,
               l.sale_krw, l.net_margin_krw, l.net_margin_pct, l.list_url,
               p.id product_id, p.asin, p.group_master_asin, p.title_ko,
               (SELECT COUNT(*) FROM listing_options o WHERE o.listing_id = l.id) option_total,
               (SELECT COUNT(*) FROM listing_options o WHERE o.listing_id = l.id
                  AND o.channel_option_id IS NOT NULL AND o.channel_option_id <> '') option_with_id
          FROM listings_pa l LEFT JOIN products p ON p.id = l.product_id
         WHERE %s ORDER BY l.id DESC LIMIT ? OFFSET ?
    """ % " AND ".join(where)

    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(sql, args + [limit, offset])]
        total = conn.execute(
            "SELECT COUNT(*) c FROM listings_pa l LEFT JOIN products p ON p.id=l.product_id"
            " WHERE %s" % " AND ".join(where), args).fetchone()["c"]

    for r in rows:
        # ★'파는 중인가'는 우리 판정이 아니라 채널 원문으로 답한다.
        r["selling"] = r["channel_status"] in SELLING if r["channel_status"] else None
        r["option_gap"] = (r["option_total"] or 0) - (r["option_with_id"] or 0)
        # 그룹이면 자식이 몇인지가 곧 옵션 수다
        r["is_group"] = bool(r["group_master_asin"])
    return {"total": total, "rows": rows, "limit": limit, "offset": offset}


@router.get("/listings/summary")
def listings_summary(user: dict = Depends(current_user)):
    """채널 × 계정 요약 — 탭 배지에 쓴다."""
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT l.channel,
                   COALESCE(l.acct_key, l.coupang_account, l.naver_account) account,
                   COUNT(*) total,
                   SUM(CASE WHEN l.status NOT IN (%s) THEN 1 ELSE 0 END) live,
                   SUM(CASE WHEN l.channel_status IN (%s) THEN 1 ELSE 0 END) selling,
                   SUM(CASE WHEN l.status='listed' AND l.channel_status IS NOT NULL
                             AND l.channel_status NOT IN (%s) THEN 1 ELSE 0 END) drift,
                   SUM(CASE WHEN l.channel_checked_at IS NULL THEN 1 ELSE 0 END) unchecked
              FROM listings_pa l
             WHERE l.channel_product_id IS NOT NULL
             GROUP BY l.channel, account ORDER BY l.channel, account
        """ % (",".join("?" * len(DEAD)), ",".join("?" * len(SELLING)),
               ",".join("?" * len(SELLING))),
            list(DEAD) + list(SELLING) + list(SELLING))]
    return {"rows": rows}
