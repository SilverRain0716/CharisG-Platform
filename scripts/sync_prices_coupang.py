"""쿠팡 가격 동기화 — DB sale_krw → 쿠팡 vendor-item PUT prices 일괄 반영.

대상:
  listings_pa (channel='coupang', status='listed', sale_krw > 0) 중 product 가
  최근 backfill 로 amazon_price_usd 갱신된 케이스. 단순 통과 sync 가 아니라
  "최근 갱신분" 만 처리해 호출 부담 최소화.

동작:
  1. 대상 listings 수집 (필요 시 --updated-since 로 시점 한정)
  2. 각 listing 의 channel_product_id (sellerProductId) 로 vendor-item id 들 조회
  3. 각 vid 에 update_vendor_item_price(vid, sale_krw) 호출
  4. 성공 시 listings_pa.last_synced_at 갱신
  5. 임시저장(statusName='임시저장') 도 포함 — vendor-item PUT 자체는 가능

사용법:
  python3 scripts/sync_prices_coupang.py --dry-run [--updated-since "2026-05-07"]
  python3 scripts/sync_prices_coupang.py --apply --updated-since "2026-05-07"
  python3 scripts/sync_prices_coupang.py --apply --limit 5

주의:
  - 단일 옵션(has_options=0) listings 만 처리. has_options=1 은 listing_options 별 PUT 필요 (별도 작업).
  - rate-limit: 호출 1건당 GET(seller-product) + N×PUT(prices). sleep 0.4s/건.
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

from backend.purchase.services.coupang_service import (
    get_seller_product, update_vendor_item_price,
)

DB_PATH = "backend/purchase/purchase.db"


def collect(updated_since: str, limit: int):
    conn = sqlite3.connect(DB_PATH, timeout=180)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=180000")

    where = [
        "l.channel = 'coupang'",
        "l.status = 'listed'",
        "l.has_options = 0",
        "l.sale_krw > 0",
        "p.amazon_price_usd > 0",
        "l.channel_product_id IS NOT NULL",
        "l.channel_product_id != ''",
    ]
    params = []
    if updated_since:
        where.append("p.updated_at >= ?")
        params.append(updated_since)
    sql = f"""SELECT l.id, l.channel_product_id, l.sale_krw, p.asin, p.title_en
              FROM listings_pa l JOIN products p ON p.id = l.product_id
              WHERE {' AND '.join(where)}
              ORDER BY l.id"""
    if limit:
        sql += f" LIMIT {limit}"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def run(apply_changes: bool, updated_since: str, limit: int):
    rows = collect(updated_since, limit)
    total = len(rows)
    print(f"대상: {total} listings  |  필터: updated_since={updated_since or '(전체)'}")
    print(f"모드: {'APPLY (외부 PUT + DB last_synced_at 갱신)' if apply_changes else 'DRY-RUN (대상만 출력)'}")
    print("=" * 110)

    if not apply_changes:
        for r in rows[:30]:
            print(f"  #{r['id']} {r['asin']}  cpid={r['channel_product_id']}  sale=₩{r['sale_krw']:,}  {(r['title_en'] or '')[:50]}")
        if total > 30:
            print(f"  ... ({total - 30} 건 더)")
        return

    # APPLY
    conn = sqlite3.connect(DB_PATH, timeout=180)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=180000")
    conn.isolation_level = None  # autocommit

    stats = {"checked": 0, "ok": 0, "fail": 0, "no_vid": 0}
    fails = []

    for idx, r in enumerate(rows, 1):
        stats["checked"] += 1
        cpid = r["channel_product_id"]
        sale = int(r["sale_krw"])
        title = (r["title_en"] or "")[:40]

        sp = get_seller_product(cpid)
        data = (sp or {}).get("data") or {}
        items = data.get("items") or []
        if not items:
            stats["no_vid"] += 1
            print(f"[{idx}/{total}] · #{r['id']} {r['asin']} cpid={cpid} → vendor-item 없음  {title}")
            time.sleep(0.4)
            continue

        # 모든 vendor-item 에 같은 sale 적용 (단일 옵션이라 보통 1개)
        item_results = []
        for it in items:
            vid = str(it.get("vendorItemId") or "")
            if not vid:
                continue
            ok, msg = update_vendor_item_price(vid, sale)
            item_results.append((vid, ok, msg))

        if item_results and all(ok for _, ok, _ in item_results):
            stats["ok"] += 1
            conn.execute(
                "UPDATE listings_pa SET last_synced_at = datetime('now') WHERE id = ?",
                (r["id"],),
            )
            vstr = ",".join(vid for vid, _, _ in item_results)
            print(f"[{idx}/{total}] ✓ #{r['id']} {r['asin']}  ₩{sale:,}  vid=[{vstr}]  {title}")
        else:
            stats["fail"] += 1
            fails.append({"id": r["id"], "asin": r["asin"], "cpid": cpid, "results": item_results, "title": title})
            print(f"[{idx}/{total}] ✗ #{r['id']} {r['asin']}  ₩{sale:,}  → {item_results}  {title}")

        time.sleep(0.4)

    conn.close()

    print()
    print("=" * 110)
    print(f"확인: {stats['checked']}  성공: {stats['ok']}  실패: {stats['fail']}  vendor-item 없음: {stats['no_vid']}")
    if fails:
        print(f"\n=== 실패 TOP 30 ===")
        for f in fails[:30]:
            r0 = f["results"][0] if f["results"] else (None, None, None)
            print(f"  #{f['id']} {f['asin']} cpid={f['cpid']} → {r0}  {f['title']}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    p.add_argument("--updated-since", type=str, default=None,
                   help="products.updated_at >= 이 값 (ISO 형식, 예: '2026-05-07')")
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()

    run(bool(args.apply), args.updated_since, args.limit)
