"""no_price 부채 청소 — 매입가 fetch 실패한 product 들의 listing 일괄 판매중지.

대상:
  amazon_price_usd IS NULL OR = 0 인 product (backfill_amazon_price.py 가
  SP-API 에서 가격을 못 받은 케이스 — 단종/제한 매물 의심) 의 listings_pa
  중 status='listed' 인 것.

동작 (채널별):
  - coupang:
      coupang_service.stop_sales(seller_product_id)
        → 내부적으로 GET seller-products/{id} 후 각 vendorItem stop
      성공 시 listings_pa.status='paused', last_synced_at 갱신
  - smartstore:
      naver_commerce_service.stop_sales(origin_product_no)
        → update_product(statusType='SUSPENSION')
      성공 시 listings_pa.status='paused', last_synced_at 갱신

사용법:
  python3 scripts/stop_sales_no_price.py --dry-run
  python3 scripts/stop_sales_no_price.py --apply [--channel coupang|smartstore|all]
  python3 scripts/stop_sales_no_price.py --apply --limit 10   # 상위 10건만 (테스트)
"""
import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, ".")

with open(".env") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from backend.purchase.services.coupang_service import stop_sales as coupang_stop_sales
from backend.purchase.services.naver_commerce_service import stop_sales as naver_stop_sales

DB_PATH = "backend/purchase/purchase.db"
COMMIT_INTERVAL = 50


def collect_targets(channel_filter: str = "all", limit: int = 0):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    where = [
        "(p.amazon_price_usd IS NULL OR p.amazon_price_usd = 0)",
        "p.business_model = 'purchase'",
        "p.status != 'removed'",
        "p.asin IS NOT NULL",
        "p.asin != ''",
        "l.status = 'listed'",
        "l.channel_product_id IS NOT NULL",
        "l.channel_product_id != ''",
    ]
    params = []
    if channel_filter != "all":
        where.append("l.channel = ?")
        params.append(channel_filter)

    sql = f"""SELECT l.id AS listing_id, l.channel, l.channel_product_id,
                     l.product_id, p.asin, p.title_en
              FROM listings_pa l
              JOIN products p ON p.id = l.product_id
              WHERE {' AND '.join(where)}
              ORDER BY l.channel, l.id"""
    if limit:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def run(apply_changes: bool, channel_filter: str, limit: int):
    rows = collect_targets(channel_filter, limit)
    total = len(rows)

    by_ch: dict[str, int] = {}
    for r in rows:
        by_ch[r["channel"]] = by_ch.get(r["channel"], 0) + 1

    print(f"대상 listings: {total}건")
    for ch, n in by_ch.items():
        print(f"  {ch}: {n}")
    print(f"모드: {'APPLY (외부 API + DB 갱신)' if apply_changes else 'DRY-RUN (대상만 출력)'}")
    print("=" * 100)

    if not apply_changes:
        # 미리보기만
        for r in rows[:50]:
            print(f"  [{r['channel']:10s}] listing #{r['listing_id']}  {r['asin']}  cpid={r['channel_product_id']}  {(r['title_en'] or '')[:60]}")
        if total > 50:
            print(f"  ... ({total - 50} 건 더)")
        return

    # APPLY
    conn = sqlite3.connect(DB_PATH, timeout=180)
    conn.row_factory = sqlite3.Row
    # 백엔드 PA API 동시 쓰기와의 락 충돌 완화 — 3분까지 양보
    conn.execute("PRAGMA busy_timeout=180000")
    conn.isolation_level = None  # autocommit — UPDATE 1건당 즉시 commit, 락 시간 최소화

    stats = {"checked": 0, "ok": 0, "fail": 0, "skip_pause": 0}
    failures = []

    for idx, r in enumerate(rows, 1):
        stats["checked"] += 1
        ch = r["channel"]
        cpid = r["channel_product_id"]
        listing_id = r["listing_id"]
        title = (r["title_en"] or "")[:50]

        try:
            if ch == "coupang":
                ok, msg = coupang_stop_sales(cpid)
                # rate-limit: 쿠팡 보수 1 req/s 수준
                time.sleep(0.6)
            elif ch == "smartstore":
                ok, msg = naver_stop_sales(cpid)
                # rate-limit 은 _gate 가 처리
            else:
                ok, msg = False, f"unknown channel {ch}"
        except Exception as e:
            ok, msg = False, f"{type(e).__name__}: {str(e)[:200]}"

        if ok:
            stats["ok"] += 1
            conn.execute(
                """UPDATE listings_pa SET
                     status         = 'paused',
                     last_synced_at = datetime('now')
                   WHERE id = ?""",
                (listing_id,),
            )
            print(f"[{idx}/{total}] ✓ {ch:10s} #{listing_id} {r['asin']}  → paused  {title}")
        else:
            stats["fail"] += 1
            failures.append({**r, "msg": msg})
            print(f"[{idx}/{total}] ✗ {ch:10s} #{listing_id} {r['asin']}  → 실패: {msg[:100]}  {title}")

    conn.close()

    print()
    print("=" * 100)
    print(f"확인: {stats['checked']}  성공: {stats['ok']}  실패: {stats['fail']}")
    if failures:
        print()
        print("=== 실패 리스트 (TOP 30) ===")
        for f in failures[:30]:
            print(f"  {f['channel']:10s} #{f['listing_id']} {f['asin']} {f['channel_product_id']}: {f['msg'][:120]}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="대상 미리보기만")
    g.add_argument("--apply", action="store_true", help="실제 외부 API + DB 갱신")
    p.add_argument("--channel", choices=["coupang", "smartstore", "all"], default="all")
    p.add_argument("--limit", type=int, default=0, help="상위 N 건만 (테스트)")
    args = p.parse_args()

    run(bool(args.apply), args.channel, args.limit)
