"""PA Dashboard — 퍼널 + 할 일 + KPI + 알림."""
from fastapi import APIRouter, Depends, Query

from backend.purchase.auth import current_user
from backend.purchase.database import get_db, get_db_hot
from backend.purchase.listing_status import DEAD as _DEAD, SELLING as _SELLING

router = APIRouter(prefix="/api/pa", tags=["pa-dashboard"])


@router.get("/dashboard")
def get_dashboard(
    channel: str | None = Query(None, description="coupang·smartstore·elevenst·auction·gmarket"),
    account: str | None = Query(None, description="old | new"),
    user: dict = Depends(current_user),
):
    """채널·계정 스코프를 받는다.

    ★리스팅·옵션·주문만 스코프가 걸린다. 키워드·소싱·통관은 채널 이전 단계라
      채널이 없다 — 억지로 0 으로 만들면 거짓말이 된다. scope.applies 로 구분해 알린다.
    """
    # listings_pa 용 조건. 계정 컬럼이 채널마다 달라 COALESCE 로 모은다.
    # ★별칭 있는 판과 없는 판을 따로 만든다. 문자열에 "l." 을 붙이는 식으로 하면
    #   COALESCE(...) 같은 조건에서 l.COALESCE(...) 가 돼 문법 오류가 난다(실측).
    def _cond(p=""):
        w, a = [], []
        if channel:
            w.append("%schannel = ?" % p)
            a.append(channel)
        if account:
            w.append("COALESCE(%sacct_key, %scoupang_account, %snaver_account) = ?" % (p, p, p))
            a.append(account)
        return ((" AND " + " AND ".join(w)) if w else ""), a

    L, la = _cond()        # listings_pa 에 직접 붙일 때
    LP, _ = _cond("l.")    # 별칭 l 일 때 — 인자는 같다

    # cold.db (대량 처리 데이터)
    with get_db() as conn:
        kw = conn.execute("SELECT COUNT(*) c FROM keywords").fetchone()["c"]
        sourcing = conn.execute("SELECT COUNT(*) c FROM sourcing_candidates").fetchone()["c"]
        # margin_calcs 는 옛 sourcing 흐름의 부산물 — 현 워크플로우는 listings_pa.net_margin_* 를 진실원으로 사용
        margin_done = conn.execute(
            "SELECT COUNT(*) c FROM listings_pa WHERE net_margin_pct IS NOT NULL" + L, la
        ).fetchone()["c"]
        customs_pass = conn.execute(
            "SELECT COUNT(*) c FROM customs_checks WHERE risk='PASS'"
        ).fetchone()["c"]
        go = conn.execute(
            "SELECT COUNT(*) c FROM sourcing_candidates WHERE sourcing_status='go'"
        ).fetchone()["c"]
        listed = conn.execute("SELECT COUNT(*) c FROM listings_pa WHERE status='listed'" + L, la).fetchone()["c"]

        # ★채널 기준 판매중 (2026-08-15). 위의 listed 는 **우리 판정**이라 부풀 수 있다.
        #   실측: DB listed=4 인데 채널에서는 1건만 팔리고 있었다
        #   (상품삭제 1 · 임시저장 2). 이 차이를 안 보여주면 잘못된 결정을 부른다.
        selling = conn.execute(
            "SELECT COUNT(*) c FROM listings_pa WHERE channel_status IN (%s)"
            % ",".join("'%s'" % x for x in _SELLING) + L, la
        ).fetchone()["c"]
        drift = conn.execute(
            "SELECT COUNT(*) c FROM listings_pa"
            " WHERE status='listed' AND channel_status IS NOT NULL"
            "   AND channel_status NOT IN (%s)"
            % ",".join("'%s'" % x for x in _SELLING) + L, la
        ).fetchone()["c"]
        never_checked = conn.execute(
            "SELECT COUNT(*) c FROM listings_pa"
            " WHERE channel_product_id IS NOT NULL AND channel_checked_at IS NULL"
            "   AND status NOT IN (%s)" % ",".join("'%s'" % x for x in _DEAD) + L, la
        ).fetchone()["c"]

        # ── 옵션 건강도 (P1-1) ──────────────────────────────
        #   주문이 왔을 때 어느 자식이 팔렸는지 특정할 수 있는가. 이게 오배송의 갈림길이다.
        opt = conn.execute("""
            SELECT COUNT(DISTINCT lo.listing_id) listings,
                   COUNT(*) rows,
                   SUM(lo.channel_option_id IS NOT NULL AND lo.channel_option_id<>'') with_id,
                   SUM(lo.child_product_id IS NOT NULL) with_child
              FROM listing_options lo JOIN listings_pa l ON l.id = lo.listing_id
             WHERE l.status NOT IN (%s)
        """ % ",".join("'%s'" % x for x in _DEAD) + LP, la).fetchone()
        try:
            queue = [dict(r) for r in conn.execute(
                "SELECT kind, COUNT(*) c FROM listing_repair_queue"
                " WHERE resolved_at IS NULL"
                + (" AND channel = ?" if channel else "")
                + " GROUP BY kind", ([channel] if channel else []))]
        except Exception:      # noqa: BLE001 — 큐 테이블이 아직 없을 수 있다
            queue = []

        n_rows = opt["rows"] or 0
        option_health = {
            "listings_with_options": opt["listings"] or 0,
            "option_rows": n_rows,
            "with_option_id": opt["with_id"] or 0,
            "with_child": opt["with_child"] or 0,
            # ★분모는 채널이 준 옵션 수여야 정확하지만, 화면용 근사로 우리 행 수를 쓴다.
            #   진짜 분모 대조는 야간 잡(nightly_option_audit)이 채널에 물어서 한다.
            "id_rate": round((opt["with_id"] or 0) / n_rows, 4) if n_rows else None,
            "repair_open": sum(q["c"] for q in queue),
            "repair_by_kind": {q["kind"]: q["c"] for q in queue},
        }
        # ★활성 상품 = **채널이 판매중이라 답한 상품 수** (2026-08-15 재정의).
        #   종전 정의(products.status)는 우리 장부라 채널과 대조된 적이 없었다 —
        #   112건 중 실제 판매중은 1건이었고, 채널을 바꿔도 값이 안 변했다.
        active = conn.execute(
            "SELECT COUNT(DISTINCT l.product_id) c FROM listings_pa l"
            " WHERE l.channel_status IN (%s)"
            % ",".join("'%s'" % x for x in _SELLING) + LP, la
        ).fetchone()["c"]
        # 아직 채널에 안 물어본 것 — 0 으로 보이면 '안 팔린다'는 오해가 된다
        active_unknown = conn.execute(
            "SELECT COUNT(DISTINCT l.product_id) c FROM listings_pa l"
            " WHERE l.channel_product_id IS NOT NULL AND l.channel_checked_at IS NULL"
            "   AND l.status NOT IN (%s)" % ",".join("'%s'" % x for x in _DEAD) + LP, la
        ).fetchone()["c"]

        nogo_pending = conn.execute(
            "SELECT COUNT(*) c FROM sourcing_candidates WHERE sourcing_status='reviewed'"
        ).fetchone()["c"]
        upload_pending = conn.execute(
            "SELECT COUNT(*) c FROM upload_queue WHERE status='pending'"
        ).fetchone()["c"]

        # 평균 마진 — sale_krw 가중평균 (매출 1원당 마진율). 매출 KPI 와 의미 일치.
        avg_margin = conn.execute(
            "SELECT 100.0 * SUM(net_margin_krw) / NULLIF(SUM(sale_krw), 0) AS m "
            "FROM listings_pa "
            "WHERE status IN ('listed','active') AND net_margin_pct IS NOT NULL "
            "AND sale_krw > 0"
        ).fetchone()["m"] or 0

        # 마지막 쿠팡 주문 동기화 (batch_jobs 는 cold)
        last_sync_row = conn.execute(
            """SELECT status, phase_message, finished_at FROM batch_jobs
               WHERE job_type='coupang_order_sync'
               ORDER BY COALESCE(finished_at, started_at, created_at) DESC LIMIT 1"""
        ).fetchone()
        last_ss_sync_row = conn.execute(
            """SELECT status, phase_message, finished_at FROM batch_jobs
               WHERE job_type='smartstore_order_sync'
               ORDER BY COALESCE(finished_at, started_at, created_at) DESC LIMIT 1"""
        ).fetchone()

        # 판매처 레지스트리 (seller_accounts, 10행) — orders.account_id → 표시키 변환용.
        # 10행짜리 조회라 ATTACH 대신 dict 로 들고 간다. 행 단위 조인이 필요해지면
        # database.get_db_with_attach() 로 옮길 것.
        seller_accounts = {
            r["id"]: f"{r['platform']}:{r['account_key']}"
            for r in conn.execute(
                "SELECT id, platform, account_key FROM seller_accounts"
            ).fetchall()
        }

    # hot.db (실시간 운영 — orders, cs_tickets)
    with get_db_hot() as conn:
        cs_open = conn.execute(
            "SELECT COUNT(*) c FROM cs_tickets WHERE status='open'"
        ).fetchone()["c"]
        # KST 자정 직후엔 '오늘 (KST)' 카운트가 0 이라 사용자 혼란 — 최근 24h 로 정의 변경
        orders_24h = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE placed_at >= datetime('now', '-1 day')"
        ).fetchone()["c"]
        # 추가: KST 오늘 / 어제 분리 — 화면에서 둘 다 보여 자정 넘김 명확
        orders_today_kst = conn.execute(
            "SELECT COUNT(*) c FROM orders "
            "WHERE date(placed_at, '+9 hours') = date('now', '+9 hours')"
        ).fetchone()["c"]
        orders_yesterday_kst = conn.execute(
            "SELECT COUNT(*) c FROM orders "
            "WHERE date(placed_at, '+9 hours') = date('now', '+9 hours', '-1 day')"
        ).fetchone()["c"]
        orders_pending = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE current_step='order_received'"
        ).fetchone()["c"]
        # 채널에 취소/출고중지 요청은 들어왔지만 우리 단계가 아직 cancelled 가 아닌 케이스.
        # 폴러가 RELEASE_STOP_UNCHECKED 같은 진행중 상태를 마킹만 하므로 사람이 셀러센터에서 처리해야 함.
        cancel_in_progress = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE canceled=0 AND cancel_status IS NOT NULL "
            "AND current_step NOT IN ('cancelled','completed')"
        ).fetchone()["c"]
        # amazon_purchase 단계인데 amazon_order_id 가 비어있는 케이스 — 정식 경로(/amazon-order) 거치지 않음.
        amazon_purchase_no_id = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE current_step='amazon_purchase' "
            "AND (amazon_order_id IS NULL OR amazon_order_id='')"
        ).fetchone()["c"]
        by_channel_rows = conn.execute(
            "SELECT channel, COUNT(*) c FROM orders GROUP BY channel"
        ).fetchall()
        orders_by_channel = {r["channel"] or "unknown": r["c"] for r in by_channel_rows}
        # 계정별(구/신) 주문 — orders.account_id(→ cold.seller_accounts) 기준.
        # ★폴백 금지: 예전 구현은 CASE 의 ELSE 가 값이 비면 'old' 로 떨어뜨려
        #   ① 계정 미상 주문을 구계정으로 단정하고
        #   ② 쿠팡이 아닌 신규 채널(11번가 등)까지 naver_account 를 읽어 오분류했다.
        #   매핑되지 않은 주문은 '<channel>:unknown' 으로 드러내고 추측하지 않는다.
        by_acct_rows = conn.execute(
            "SELECT account_id, channel, COUNT(*) c FROM orders GROUP BY account_id, channel"
        ).fetchall()
        orders_by_account: dict[str, int] = {}
        for r in by_acct_rows:
            key = seller_accounts.get(r["account_id"]) or f"{r['channel'] or 'unknown'}:unknown"
            orders_by_account[key] = orders_by_account.get(key, 0) + r["c"]
    last_coupang_sync = dict(last_sync_row) if last_sync_row else None
    last_smartstore_sync = dict(last_ss_sync_row) if last_ss_sync_row else None

    # funnel — 데이터 있는 단계만 표시. 옛 sourcing/customs/go 흐름은 PA 디스커버리 풀
    # 파이프라인 재설계 후 미사용 → 0 으로만 보이는 단계는 비공개. 필요 시 모두 보이게 하려면
    # ?include_empty=1 쿼리로 확장 가능.
    funnel_full = [
        {"key": "keywords",  "label": "키워드",   "count": kw},
        {"key": "sourcing",  "label": "소싱",    "count": sourcing},
        {"key": "margin",    "label": "마진산정", "count": margin_done},
        {"key": "customs",   "label": "통관",    "count": customs_pass},
        {"key": "go",        "label": "GO",      "count": go},
        {"key": "listed",    "label": "등록",    "count": listed},
        {"key": "active",    "label": "활성",    "count": active},
    ]
    funnel = [s for s in funnel_full if s["count"] > 0]

    return {
        "funnel": funnel,
        "todos": {
            "go_pending": nogo_pending,
            "upload_pending": upload_pending,
            "cs_open": cs_open,
            "orders_pending": orders_pending,
            "cancel_in_progress": cancel_in_progress,
            "amazon_purchase_no_id": amazon_purchase_no_id,
        },
        "kpis": {
            "active_products": active,
            # ★대조 전이라 판정 못 한 상품. active_products 와 나란히 봐야 오해가 없다.
            "active_unknown": active_unknown,
            "avg_margin": round(avg_margin, 1),
            "orders_24h": orders_24h,                  # 최근 24시간 결제 (자정 넘김 무관)
            "orders_today_kst": orders_today_kst,      # KST 오늘 결제
            "orders_yesterday_kst": orders_yesterday_kst,  # KST 어제 결제
            "orders_today": orders_24h,                # 호환: 기존 'orders_today' 키는 24h 값으로 매핑
            "orders_by_channel": orders_by_channel,
            # ★channel_* 는 채널이 진실이다. listed 는 우리 판정이라 나란히 둔다.
            "selling_on_channel": selling,
            "status_drift": drift,
            "never_checked": never_checked,
            "option_health": option_health,
            # ★무엇에 스코프가 걸렸는지 화면이 알아야 한다. 안 걸린 값을 채널별로 읽으면 오해한다.
            "scope": {
                "channel": channel, "account": account,
                "applies": ["listed", "selling_on_channel", "status_drift", "never_checked",
                            "option_health", "margin_done"],
                "global": ["keywords", "sourcing", "customs_pass", "orders_*"],
            },
            "orders_by_account": orders_by_account,   # 'smartstore:old' 같은 키
        },
        "last_coupang_sync": last_coupang_sync,
        "last_smartstore_sync": last_smartstore_sync,
        "alerts": [],
    }
