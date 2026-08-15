# -*- coding: utf-8 -*-
"""channel_guard.py — 채널 정합성 점검 에이전트 1단계 (2026-08-08 신설).

왜 만드는가
-----------
2026-08-08 하루에만 "조용히 쌓이다 주문이 들어와서야 발견된" 사고가 연달아 나왔다.

  · listing_options.channel_option_id 48,514건 미기록 → 옵션 주문이 형제 상품으로
    폴백되어 오배송 4건(1221/1647/1677/1730). 그중 2건은 이미 발송 완료.
  · 그 값을 채우는 sync 스크립트는 만들어져 있었으나 크론 미등록으로 한 번도 안 돎.
  · 게다가 계정 분기가 없어 돌렸어도 신계정 7,885건은 전부 실패했을 것.

전부 "누가 주기적으로 봤으면 안 터졌을" 것들이다. 이 스크립트는 그 '주기적으로 보는
사람' 역할을 한다.

설계 원칙
---------
1. **AI 0원.** 판단이 아니라 대조다. 전부 규칙과 집계 쿼리로 끝난다.
2. **가벼운 쿼리.** 인덱스 있는 컬럼으로만 걸고 COUNT 집계를 쓴다. 전 테이블 로드 금지.
   샘플은 LIMIT 로 소량만 뜬다.
3. **변화만 보고.** 매번 같은 숫자를 나열하면 아무도 안 읽는다. 직전 실행 대비
   '새로 생겼거나 나빠진 것'만 강조한다.
4. **끊기지 않는다.** 한 점검이 실패해도 나머지는 계속 돈다. 감시자가 죽으면
   감시 대상이 죽은 것보다 나쁘다.

사용:
  python -m backend.purchase.scripts.channel_guard            # 점검 + 델타
  python -m backend.purchase.scripts.channel_guard --notify   # 이상 있으면 텔레그램
  python -m backend.purchase.scripts.channel_guard --all      # 정상 항목까지 전부 출력
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform"))
COLD = ROOT / "backend/purchase/purchase.db"
HOT = ROOT / "backend/purchase/purchase_hot.db"
SNAP = ROOT / "snapshots" / "channel_guard.json"

# 채널 등록 한도 — 공식 확인값. 넘으면 신규 등록이 막힌다.
#   네이버 1,000 / 11번가 5등급 5,000. 쿠팡은 실질 무제한.
LIMITS = {"smartstore": 1000, "elevenst": 5000}

# 멀티계정 채널 — 한도도 옵션도 계정 단위로 봐야 한다.
# 표현식은 listings_pa 별칭 l 을 전제로 한다.
_ACCT_EXPR = {
    "smartstore": "COALESCE(NULLIF(l.naver_account,''),'old')",
    "coupang": "COALESCE(NULLIF(l.coupang_account,''),'old')",
}
_ACCT_LABEL = {
    ("smartstore", "old"): "구/카리스G", ("smartstore", "new"): "신/카리스글로벌",
    ("coupang", "old"): "구/카리스G", ("coupang", "new"): "신/카리스글로벌",
}

OK, WARN, CRIT = "정상", "주의", "심각"


def _ro(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


class Report:
    def __init__(self):
        self.rows = []

    def add(self, key, level, value, msg, sample=None):
        self.rows.append({"key": key, "level": level, "value": value,
                          "msg": msg, "sample": sample or []})


def check(fn):
    """점검 하나가 터져도 전체는 계속 돈다."""
    def wrap(rep, *a, **k):
        try:
            fn(rep, *a, **k)
        except Exception as e:  # noqa: BLE001
            rep.add(f"{fn.__name__}:실패", WARN, -1, f"점검 자체가 실패: {str(e)[:120]}")
    return wrap


# ── 점검 ────────────────────────────────────────────────────────────

@check
def chk_option_ids(rep, cold):
    """옵션ID 미기록 — 비면 옵션 주문이 그룹 대표로 폴백해 오배송이 난다."""
    # ★계정별로 나눈다. 합치면 한쪽을 다 고쳐도 다른 쪽에 묻혀 진척이 안 보인다.
    rows = cold.execute("""
        SELECT l.channel,
               CASE WHEN l.channel='smartstore'
                    THEN COALESCE(NULLIF(l.naver_account,''),'old')
                    WHEN l.channel='coupang'
                    THEN COALESCE(NULLIF(l.coupang_account,''),'old')
                    ELSE '' END acct,
               SUM(CASE WHEN o.channel_option_id IS NULL OR o.channel_option_id='' THEN 1 ELSE 0 END) bad,
               SUM(CASE WHEN (o.channel_option_id IS NULL OR o.channel_option_id='')
                             AND l.status='listed' THEN 1 ELSE 0 END) bad_live,
               COUNT(*) tot
        FROM listing_options o JOIN listings_pa l ON l.id=o.listing_id
        WHERE l.status IN ('listed','paused')
        GROUP BY l.channel, acct""").fetchall()
    for r in rows:
        if not r["tot"]:
            continue
        pct = 100.0 * r["bad"] / r["tot"]
        # ★심각도는 '판매중'인 건으로만 정한다. 판매중지한 건은 주문이 들어올 수 없어
        #   위험이 없는데, 비율만 보면 100% 로 남아 영구히 심각을 울린다(조치했는데도
        #   경보가 안 꺼지면 다음부터 아무도 안 본다).
        lvl = (CRIT if pct >= 30 else WARN) if r["bad_live"] else (WARN if r["bad"] else OK)
        tag = _ACCT_LABEL.get((r["channel"], r["acct"]))
        name = f"옵션ID미기록:{r['channel']}" + (f"({tag})" if tag else "")
        rep.add(name, lvl, r["bad"],
                f"{r['bad']:,}/{r['tot']:,} ({pct:.1f}%) — 비면 옵션 주문이 형제 상품으로 폴백")


@check
def chk_option_unmappable(rep, cold):
    """옵션ID·옵션명이 둘 다 빈 리스팅 — 주문이 오면 매칭 폴백이 전부 막힌다.

    주문→자식 매칭은 3단계다: channel_option_id → optionManageCode → channel_option_name.
    1·3 이 동시에 비면 남는 건 2번뿐인데, 네이버 단독형으로 저장된 건은 그 값도 없다.
    ★2026-04-24 사고: 조합형으로 보냈는데 네이버가 단독형으로 저장 → 4개월간 '정상'으로
      기록돼 있었다. 개수만 세는 위 점검과 달리 '매칭 자체가 불가능한' 건을 따로 센다.
    """
    # ★옵션이 2개 이상인 리스팅만 진짜 오배송이다. 옵션이 1개면 보낼 것이 하나뿐이라
    #   매칭이 안 돼도 잘못 나갈 수가 없다(주문은 '자식 미확정'으로 남아 별도 점검에 걸린다).
    #   둘을 같은 심각도로 올리면 알림이 무뎌져서 진짜 건을 놓친다.
    rows = cold.execute("""
        WITH bad AS (
            SELECT l.id lid, l.channel, l.status,
                   CASE WHEN l.channel='smartstore'
                        THEN COALESCE(NULLIF(l.naver_account,''),'old')
                        ELSE COALESCE(NULLIF(l.coupang_account,''),'old') END acct,
                   COUNT(*) opts
            FROM listing_options o JOIN listings_pa l ON l.id=o.listing_id
            WHERE l.status IN ('listed','paused')
              AND (o.channel_option_id IS NULL OR o.channel_option_id='')
              AND (o.channel_option_name IS NULL OR o.channel_option_name='')
            GROUP BY l.id
        )
        SELECT channel, acct,
               COUNT(*) listings,
               SUM(CASE WHEN opts > 1 THEN 1 ELSE 0 END) multi,
               SUM(CASE WHEN opts > 1 AND status='listed' THEN 1 ELSE 0 END) multi_live
        FROM bad GROUP BY channel, acct""").fetchall()
    for r in rows:
        if not r["listings"]:
            continue
        tag = _ACCT_LABEL.get((r["channel"], r["acct"]))
        name = f"옵션 매칭불가:{r['channel']}" + (f"({tag})" if tag else "")
        # ★심각으로 올리지 않는다. 채널이 돌려주는 외부 SKU(자식 ASIN) 폴백이
        #   대부분을 복원하기 때문이다(order_child_resolve, 2026-08-08). 실측에서도
        #   옵션 주문 31건 중 실제 미상은 3건뿐이었다. 진짜 피해는 주문 쪽 점검
        #   (자식 미상 주문)이 센다 — 여기서 심각을 남발하면 그게 묻힌다.
        rep.add(name, WARN if r["multi"] else OK, r["multi"],
                f"리스팅 {r['listings']:,}건 중 옵션 2개 이상 {r['multi']:,}건"
                f"(그중 판매중 {r['multi_live']:,}) — 옵션ID·옵션명이 비어 "
                f"외부 SKU 폴백에만 의존한다. SKU 마저 없으면 자식 특정 불가")


@check
def chk_order_child_unknown(rep, hot, cold):
    """옵션 상품 주문인데 자식을 특정할 단서가 하나도 없는 건 — 실제 오배송.

    옵션 주문의 자식 확정 경로는 셋이다.
      1) listing_options.channel_option_id (vendorItemId)
      2) 외부 SKU 가 자식 ASIN(B0…) → order_child_resolve
      3) 외부 SKU 가 'PA-{자기 product_id}' → 마스터 옵션 주문이라 부모 폴백이 정답
    셋 다 없으면(=SKU 자체가 없음) 어느 형제를 보낼지 알 방법이 없다.

    ★리스팅 수를 세는 위 점검과 달리 '실제로 팔린 것'만 센다. 실측(2026-08-10):
      옵션 주문 31건 중 1)6건 2)0건 3)22건, 단서 없음 3건.
    """
    opt_pids = {r["product_id"] for r in cold.execute(
        "SELECT DISTINCT product_id FROM listings_pa WHERE has_options=1")}
    if not opt_pids:
        rep.add("자식 미상 주문", OK, 0, "옵션 상품 없음")
        return
    # ★취소된 주문은 아무것도 나가지 않았으므로 위험이 아니다. 이걸 심각으로 세면
    #   과거 취소건이 매일 알림을 울려 진짜 진행 중인 건이 묻힌다.
    live, dead = [], []
    for o in hot.execute(
        "SELECT id, channel, product_id, external_sku, placed_at, current_step, canceled "
        "FROM orders WHERE child_product_id IS NULL AND product_id IS NOT NULL "
        "  AND (external_sku IS NULL OR external_sku='')"
    ):
        if o["product_id"] not in opt_pids:
            continue
        terminal = o["canceled"] or (o["current_step"] or "").lower() in ("cancelled", "canceled")
        (dead if terminal else live).append(o)

    rep.add("자식 미상 주문", CRIT if live else OK, len(live),
            f"진행 중 {len(live)}건 (취소·종료 {len(dead)}건은 제외) — 옵션 상품 주문인데 "
            f"외부 SKU 가 없어 어느 형제인지 알 수 없다. 발송 전 수동 확인 필요",
            [f"주문 {o['id']} · pid {o['product_id']} · {str(o['placed_at'])[:10]} "
             f"({o['current_step']})" for o in live[:5]])


@check
def chk_order_mapping(rep, hot):
    """자식 미확정 주문 — 어느 옵션이 팔렸는지 모르는 채로 남아 있는 건."""
    r = hot.execute("""
        SELECT COUNT(*) n FROM orders
        WHERE (child_product_id IS NULL OR child_product_id='')
          AND external_sku IS NOT NULL AND external_sku!=''
          AND external_sku LIKE 'B0%' AND length(external_sku)=10""").fetchone()
    n = r["n"] if r else 0
    smp = [dict(x) for x in hot.execute("""
        SELECT id, channel, external_sku, current_step FROM orders
        WHERE (child_product_id IS NULL OR child_product_id='')
          AND external_sku LIKE 'B0%' AND length(external_sku)=10
        ORDER BY id DESC LIMIT 5""").fetchall()]
    rep.add("주문 자식미확정", CRIT if n else OK, n,
            f"{n}건 — 외부SKU 는 있는데 옵션이 안 붙었다(오배송 직결)",
            [f"주문{s['id']} {s['external_sku']} {s['current_step']}" for s in smp])


@check
def chk_limits(rep, cold):
    """채널 등록 한도 — 넘으면 신규 등록이 막힌다."""
    # ★한도는 계정마다 따로 걸린다. 합치면 '초과'로 보이는데 실제로는 한쪽만 찼고
    #   다른 쪽엔 자리가 남아 있는 상황을 정반대로 읽게 된다.
    targets = []
    for ch, cap in LIMITS.items():
        expr = _ACCT_EXPR.get(ch)
        if not expr:
            targets.append((ch, cap, "", "1=1", ()))
            continue
        accts = [x[0] for x in cold.execute(
            f"SELECT DISTINCT {expr} a FROM listings_pa l WHERE l.channel=?", (ch,)
        ).fetchall()]
        for a in sorted(accts or ["old"]):
            targets.append((ch, cap, _ACCT_LABEL.get((ch, a), a), f"{expr}=?", (a,)))

    for ch, cap, tag, acct_sql, acct_p in targets:
        suffix = f"({tag})" if tag else ""
        r = cold.execute(
            f"SELECT COUNT(*) n FROM listings_pa l WHERE l.channel=? AND l.status='listed' "
            f"AND {acct_sql}", (ch, *acct_p)
        ).fetchone()
        n = r["n"] if r else 0
        pct = 100.0 * n / cap
        # ★DB 의 listed 는 채널 실제와 어긋날 수 있다(실측: 네이버 listed 5,734 중
        #   5,294건이 2026-06 이후 미동기화). 초과로 단정하지 않고 '확인 필요'로 낸다.
        stale = cold.execute(f"""
            SELECT COUNT(*) n FROM listings_pa l
            WHERE l.channel=? AND l.status='listed' AND {acct_sql}
              AND (l.last_synced_at IS NULL OR l.last_synced_at < date('now','-30 day'))""",
            (ch, *acct_p)).fetchone()["n"]
        lvl = WARN if (n > cap or pct >= 85) else OK
        note = ""
        if n > cap:
            note = (f" ★DB 기준 한도 초과 — 다만 30일 이상 미동기화가 {stale:,}건이라 "
                    f"실제 채널 등록수는 이보다 적을 가능성이 크다. 대조 필요")
        rep.add(f"등록한도:{ch}{suffix}", lvl, n, f"{n:,}/{cap:,} ({pct:.0f}%)" + note)
        if stale:
            rep.add(f"동기화 정체:{ch}{suffix}", WARN if stale > n * 0.3 else OK, stale,
                    f"{stale:,}건이 30일 이상 미동기화 — DB 와 채널 실제가 벌어진다")


@check
def chk_blocked_alive(rep, cold):
    """차단 판정을 받았는데 아직 살아있는 리스팅 — KC·브랜드IP 리스크."""
    # (a) 진짜 생존 — 삭제 기록이 없거나, 삭제 후 다시 등록된 것
    live = cold.execute("""
        SELECT b.axis, COUNT(*) n FROM blocked_products b
        JOIN products p ON p.asin=b.asin
        JOIN listings_pa l ON l.product_id=p.id AND l.status='listed'
        WHERE b.deleted_at IS NULL OR b.deleted_at='' OR l.created_at > b.deleted_at
        GROUP BY b.axis""").fetchall()
    n_live = sum(r["n"] for r in live)
    rep.add("차단대상 생존", CRIT if n_live else OK, n_live,
            f"{n_live}건 — 삭제 기록이 없거나 삭제 후 재등록됐다(실제 리스크)",
            [f"{r['axis']}: {r['n']}건" for r in live])

    # (b) status 드리프트 — 채널에선 지웠는데 DB 만 listed
    drift = cold.execute("""
        SELECT COUNT(*) n FROM blocked_products b
        JOIN products p ON p.asin=b.asin
        JOIN listings_pa l ON l.product_id=p.id AND l.status='listed'
        WHERE b.deleted_at IS NOT NULL AND b.deleted_at!='' AND l.created_at <= b.deleted_at
    """).fetchone()["n"]
    rep.add("차단대상 status 드리프트", WARN if drift else OK, drift,
            f"{drift}건 — 채널에선 삭제됐는데 DB status 가 listed. "
            f"집계·로테이션이 이 건을 살아있는 것으로 오인한다")


@check
def chk_detail_version(rep, cold):
    """상세 템플릿 구버전 — 옛 문구가 그대로 나간다."""
    try:
        import sys
        sys.path.insert(0, str(ROOT))
        from backend.purchase.services.ai_processor import PA_TEMPLATE_VERSION as CUR
    except Exception:
        CUR = None
    if not CUR:
        rep.add("상세버전", WARN, -1, "현행 버전을 못 읽음")
        return
    r = cold.execute("""
        SELECT COUNT(*) n FROM detail_pages d
        JOIN listings_pa l ON l.product_id=d.product_id AND l.status='listed'
        WHERE COALESCE(d.template_version,'') != ?""", (CUR,)).fetchone()
    n = r["n"] if r else 0
    rep.add("상세 구버전", WARN if n else OK, n,
            f"{n:,}건이 구버전(현행 {CUR}) — 다음 사용 시 자동 재생성되나, "
            f"라이브 반영은 재전송해야 한다")


@check
def chk_price_sanity(rep, cold):
    """listings_pa.sale_krw 미채움.

    ★실측(2026-08-08): 이 건들은 products.sale_price_krw 에는 값이 있다(12만원대 등).
      즉 '가격이 없는 상품'이 아니라 리스팅 테이블에 비정규화가 안 된 상태다.
      주문이 곧바로 손해로 이어지진 않지만, sale_krw 를 읽는 마진·재가격 로직이
      이 건들을 잘못 계산하거나 건너뛴다. 그래서 심각이 아니라 주의로 본다.
    """
    r = cold.execute("""
        SELECT COUNT(*) n FROM listings_pa l
        WHERE l.status='listed' AND (l.sale_krw IS NULL OR l.sale_krw<=0)""").fetchone()
    n = r["n"] if r else 0
    # 진짜 위험한 건 products 쪽에도 가격이 없는 경우 — 이건 심각.
    r2 = cold.execute("""
        SELECT COUNT(*) n FROM listings_pa l JOIN products p ON p.id=l.product_id
        WHERE l.status='listed' AND (l.sale_krw IS NULL OR l.sale_krw<=0)
          AND (p.sale_price_krw IS NULL OR p.sale_price_krw<=0)""").fetchone()
    n2 = r2["n"] if r2 else 0
    rep.add("판매가 비정규화 누락", WARN if n else OK, n,
            f"{n:,}건 — listings_pa.sale_krw 가 비었다(products 에는 값 있음). "
            f"마진·재가격 로직이 이 건을 잘못 다룬다")
    rep.add("판매가 완전 부재", CRIT if n2 else OK, n2,
            f"{n2:,}건 — products 에도 가격이 없다(진짜 위험)")

    r2 = cold.execute("""
        SELECT COUNT(*) n FROM listings_pa
        WHERE status='listed' AND net_margin_krw > 100000""").fetchone()
    n2 = r2["n"] if r2 else 0
    rep.add("마진상한 초과", WARN if n2 else OK, n2,
            f"{n2:,}건이 절대마진 10만원 상한을 넘는다")


@check
def chk_cron_health(rep, _):
    """배치가 실제로 돌고 있는가 — 만들어두고 안 도는 게 가장 위험하다."""
    watch = {
        "옵션ID 동기화": Path("/home/ubuntu/logs/optid_sync.log"),
    }
    now = time.time()
    for name, p in watch.items():
        if not p.is_file():
            rep.add(f"배치:{name}", CRIT, -1, f"로그 없음 — 한 번도 안 돌았다 ({p})")
            continue
        age_h = (now - p.stat().st_mtime) / 3600
        lvl = CRIT if age_h > 48 else (WARN if age_h > 26 else OK)
        rep.add(f"배치:{name}", lvl, round(age_h, 1), f"마지막 실행 {age_h:.1f}시간 전")


@check
def chk_image_class(rep, _):
    """이미지 형식 분류 결손 — 상세는 만들었는데 분류 캐시가 없는 상품.

    분류가 없으면 전 이미지가 unknown 이 되어 marketing(홍보 합성컷) 배제가
    작동하지 않는다. 실측(2026-08-08): 이 상태에서 타사 비교컷이 히어로로 올라갔다.
    """
    import os
    root = ROOT / "backend/purchase/media/products"
    if not root.is_dir():
        rep.add("이미지분류 결손", WARN, -1, f"미디어 디렉터리 없음: {root}")
        return
    total = have_cls = have_detail = gap = 0
    sample = []
    with os.scandir(root) as it:
        for e in it:
            if not e.is_dir():
                continue
            total += 1
            try:
                names = set(os.listdir(e.path))
            except OSError:
                continue
            c = "image_class.json" in names
            d = "seo_detail.json" in names or any(n.startswith("agent_") for n in names)
            have_cls += c
            have_detail += d
            if d and not c:
                gap += 1
                if len(sample) < 5:
                    sample.append(f"pid {e.name}")
    rep.add("이미지분류 결손", CRIT if gap else OK, gap,
            f"{gap}건 — 상세를 만들었는데 형식 분류가 없다. "
            f"marketing(홍보 합성컷) 배제가 무력화된 상태로 나간다",
            sample)
    rep.add("이미지분류 보유율", OK, have_cls,
            f"{have_cls:,}/{total:,} 보유 · 에이전트 상세 {have_detail:,}건 (참고)")




# ── 신 파이프라인(import_*) ─────────────────────────────────────────
# ★구 파이프라인(listings_pa)과 테이블이 완전히 다르다. 위 점검들은 이 구간을 못 본다.

def _has(c, t):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone())


@check
def chk_import_risk(rep, cold):
    """리스크 3축 미판정 — M12~M14 를 안 돌고 등록하면 KC·리셀·한국브랜드가 무방비다."""
    if not _has(cold, "import_detail"):
        return
    rows = cold.execute("""
        SELECT d.batch, COUNT(*) tot,
               SUM(CASE WHEN r.n IS NULL OR r.n < 3 THEN 1 ELSE 0 END) bad
          FROM import_detail d
          LEFT JOIN (SELECT batch, asin, COUNT(DISTINCT axis) n
                       FROM import_risk GROUP BY batch, asin) r
                 ON r.batch=d.batch AND r.asin=d.asin
         GROUP BY d.batch""").fetchall()
    for r in rows:
        if not r["tot"]:
            continue
        lvl = CRIT if r["bad"] else OK
        rep.add(f"임포트:리스크3축 미판정:{r['batch'][:18]}", lvl, r["bad"],
                f"{r['bad']:,}/{r['tot']:,} — 3축(kc·resale·korean_brand) 판정이 안 끝났다")


@check
def chk_import_blocked(rep, cold):
    """3축에서 차단·보류가 난 건이 남아 있나 — 등록 전에 사람이 봐야 한다."""
    if not _has(cold, "import_risk"):
        return
    rows = cold.execute("""
        SELECT axis, verdict, COUNT(*) n FROM import_risk
         WHERE verdict IN ('차단','대상','보류','사람검토')
         GROUP BY axis, verdict""").fetchall()
    for r in rows:
        lvl = CRIT if r["verdict"] in ("차단", "대상") else WARN
        rep.add(f"임포트:{r['axis']} {r['verdict']}", lvl, r["n"],
                f"{r['n']:,}건 — 등록 전 처리 필요")


@check
def chk_import_image(rep, cold):
    """이미지 미생성 — M15 를 안 돌면 브랜드 원본이 그대로 나가 저작권에 노출된다."""
    if not _has(cold, "import_detail") or not _has(cold, "import_image"):
        return
    rows = cold.execute("""
        SELECT d.batch, COUNT(*) tot,
               SUM(CASE WHEN i.asin IS NULL THEN 1 ELSE 0 END) bad
          FROM import_detail d
          LEFT JOIN (SELECT DISTINCT batch, asin FROM import_image) i
                 ON i.batch=d.batch AND i.asin=d.asin
         GROUP BY d.batch""").fetchall()
    for r in rows:
        if not r["tot"]:
            continue
        # ★재생성은 화장품·건기식 전용이라 '미생성'이 정상인 배치가 많다 → 주의까지만
        rep.add(f"임포트:이미지 미생성:{r['batch'][:18]}", WARN if r["bad"] else OK, r["bad"],
                f"{r['bad']:,}/{r['tot']:,} — 원본 사진이 그대로 나간다(화장품·건기식이면 반드시 생성)")


@check
def chk_import_temp_products(rep, cold):
    """실체화 임시행 누적 — asin 이 UNIQUE 가 아니라 중복이 쌓이고 ASIN 조회가 헷갈린다."""
    n = cold.execute("SELECT COUNT(*) FROM products WHERE status='import_temp'").fetchone()[0]
    dup = cold.execute("""
        SELECT COUNT(*) FROM (SELECT asin FROM products WHERE asin IS NOT NULL
                               GROUP BY asin HAVING SUM(CASE WHEN status='import_temp' THEN 1 ELSE 0 END)
                                              AND COUNT(*) > 1)""").fetchone()[0]
    lvl = CRIT if dup else (WARN if n else OK)
    rep.add("임포트:임시 products", lvl, n,
            f"{n:,}행(status=import_temp) · 같은 ASIN 이 정식행과 겹친 것 {dup:,} — M18 모델 확정 시 정리")


@check
def chk_import_option_id(rep, cold):
    """옵션ID 미회수 — 구 파이프라인에서 오배송 4건을 낸 바로 그 경로다."""
    if not _has(cold, "import_option"):
        return
    r = cold.execute("""
        SELECT COUNT(*) tot,
               SUM(CASE WHEN channel_option_id IS NULL OR channel_option_id='' THEN 1 ELSE 0 END) bad,
               SUM(CASE WHEN external_sku IS NULL OR external_sku='' THEN 1 ELSE 0 END) nosku
          FROM import_option""").fetchone()
    if not r["tot"]:
        return
    # ★아직 등록 전이면 비어 있는 게 정상이다 — 등록 실적이 생긴 뒤에 심각으로 올린다
    listed = cold.execute("SELECT COUNT(*) FROM import_option"
                          " WHERE channel_product_id IS NOT NULL AND channel_product_id<>''").fetchone()[0]
    lvl = (CRIT if r["bad"] else OK) if listed else OK
    rep.add("임포트:옵션ID 미회수", lvl, r["bad"],
            f"{r['bad']:,}/{r['tot']:,} 미회수 · 외부SKU 없음 {r['nosku']:,} "
            f"(등록된 것 {listed:,}) — 등록 전이면 정상")


# ── 실행 ────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notify", action="store_true", help="이상 시 텔레그램 발송")
    ap.add_argument("--all", action="store_true", help="정상 항목까지 출력")
    # ★구축 기간엔 구 파이프라인 경보가 신 파이프라인 경보를 묻는다.
    #   DB 가 실제 채널과 어긋나 있어(전량삭제 미반영) [심각]이 상시로 뜨기 때문이다.
    #   경보를 끄지 않고 **보는 범위만** 나눈다.
    ap.add_argument("--scope", choices=("all", "legacy", "import"), default="all",
                    help="legacy=구 파이프라인만 · import=신 파이프라인만 · all=전부(기본)")
    a = ap.parse_args()

    rep = Report()
    cold, hot = _ro(COLD), _ro(HOT)
    if a.scope in ("all", "legacy"):
        chk_option_ids(rep, cold)
        chk_option_unmappable(rep, cold)
        chk_order_child_unknown(rep, hot, cold)
        chk_order_mapping(rep, hot)
        chk_limits(rep, cold)
        chk_blocked_alive(rep, cold)
        chk_detail_version(rep, cold)
        chk_price_sanity(rep, cold)
        chk_cron_health(rep, None)
        chk_image_class(rep, None)
    if a.scope in ("all", "import"):
        # ★신 파이프라인 — 위 점검들은 listings_pa 만 본다
        chk_import_risk(rep, cold)
        chk_import_blocked(rep, cold)
        chk_import_image(rep, cold)
        chk_import_temp_products(rep, cold)
        chk_import_option_id(rep, cold)

    # ★스냅샷을 범위별로 나눈다 — 안 나누면 --scope import 실행이
    #   구 파이프라인 항목을 '사라짐'으로 오인해 변화량이 깨진다.
    global SNAP
    if a.scope != 'all':
        SNAP = SNAP.with_name(SNAP.stem + '_' + a.scope + SNAP.suffix)
    prev = {}
    if SNAP.is_file():
        try:
            prev = {r["key"]: r for r in json.loads(SNAP.read_text(encoding="utf-8"))}
        except Exception:
            prev = {}

    order = {CRIT: 0, WARN: 1, OK: 2}
    rep.rows.sort(key=lambda r: (order.get(r["level"], 9), r["key"]))

    print("=" * 74)
    print("채널 정합성 점검")
    print("=" * 74)
    bad = []
    for r in rep.rows:
        if r["level"] == OK and not a.all:
            continue
        p = prev.get(r["key"])
        delta = ""
        if p and isinstance(p.get("value"), (int, float)) and isinstance(r["value"], (int, float)):
            d = r["value"] - p["value"]
            if d:
                delta = f"  ({d:+,} 지난번 대비)"
        print(f"\n[{r['level']}] {r['key']}{delta}")
        print(f"   {r['msg']}")
        for s in r["sample"][:5]:
            print(f"     · {s}")
        if r["level"] != OK:
            bad.append(r)

    if not bad:
        print("\n이상 없음.")
    else:
        print(f"\n{'=' * 74}\n심각 {sum(1 for r in bad if r['level']==CRIT)}건 / "
              f"주의 {sum(1 for r in bad if r['level']==WARN)}건")

    SNAP.parent.mkdir(parents=True, exist_ok=True)
    SNAP.write_text(json.dumps(rep.rows, ensure_ascii=False, indent=1), encoding="utf-8")

    if a.notify and bad:
        try:
            from backend.purchase.services.telegram_service import send_message as send_telegram
            lines = [f"[채널점검] 심각 {sum(1 for r in bad if r['level']==CRIT)} / "
                     f"주의 {sum(1 for r in bad if r['level']==WARN)}"]
            for r in bad[:8]:
                lines.append(f"{r['level']} {r['key']} — {r['msg'][:70]}")
            send_telegram("\n".join(lines))
            print("텔레그램 발송 완료")
        except Exception as e:  # noqa: BLE001
            print(f"텔레그램 발송 실패(무시): {str(e)[:100]}")


if __name__ == "__main__":
    main()
