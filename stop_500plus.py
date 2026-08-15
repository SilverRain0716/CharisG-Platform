"""$500 이상 원가 product 의 쿠팡 vendor-item stop_sales.

cost_usd 오류 outlier + 고가 → 의도된 차단 (사용자 요청).
"""
import sys, os, sqlite3, time
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.services.coupang_service import get_seller_product, stop_sales_vendor_item

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
conn = sqlite3.connect(DB, timeout=180)
conn.execute("PRAGMA busy_timeout=180000")
conn.row_factory = sqlite3.Row

# 500+ cost product 의 listed sellerProduct
rows = conn.execute("""
  SELECT DISTINCT p.id, p.asin, p.cost_usd, l.channel_product_id spid, l.id lid
  FROM products p
  JOIN listings_pa l ON l.product_id=p.id
  WHERE p.cost_usd >= 500 AND l.channel='coupang' AND l.status='listed'
    AND l.channel_product_id IS NOT NULL AND l.channel_product_id != ''
  ORDER BY p.cost_usd DESC
""").fetchall()
print(f"=== stop_sales 대상 {len(rows)} sellerProduct ===")

stop_ok = stop_fail = no_vid = 0
spids_done: list[str] = []
for r in rows:
    info = get_seller_product(str(r["spid"]))
    if not info or not isinstance(info.get("data"), dict):
        print(f"  spid={r['spid']} (asin {r['asin']}, ${r['cost_usd']}) — 조회 실패")
        stop_fail += 1
        time.sleep(0.3)
        continue
    items = info["data"].get("items") or []
    vid_per_sp = 0
    for it in items:
        vid = it.get("vendorItemId")
        if not vid:
            no_vid += 1
            continue
        success, msg = stop_sales_vendor_item(str(vid))
        if success:
            stop_ok += 1
            vid_per_sp += 1
        else:
            stop_fail += 1
            print(f"    STOP FAIL vid={vid}: {msg[:80]}")
        time.sleep(0.4)
    # listings_pa 마킹
    conn.execute(
        """UPDATE listings_pa SET status='paused',
           error_message='고가 원가 ($500+) 판매중지 (cost_usd outlier 차단)',
           last_synced_at=CURRENT_TIMESTAMP WHERE id=?""",
        (r["lid"],),
    )
    conn.commit()
    print(f"  spid={r['spid']} asin={r['asin']} cost=${r['cost_usd']} stop OK ({vid_per_sp} vid)")
    spids_done.append(str(r["spid"]))
    time.sleep(0.3)

print(f"\n=== 결과 stop OK {stop_ok} / fail {stop_fail} / no_vid {no_vid} ===")
print(f"sellerProduct 처리: {len(spids_done)}")
conn.close()
