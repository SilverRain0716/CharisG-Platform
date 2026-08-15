from dotenv import load_dotenv; load_dotenv()
from backend.purchase.services import coupang_service as cs
import sqlite3, time, collections

path = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
nt = ""; page = 0; items = []
while True:
    qs = f"vendorId={cs.COUPANG_VENDOR_ID}&maxPerPage=100&nextToken={nt}"
    r = cs._request_with_retry("GET", cs.BASE + path + "?" + qs,
                               headers=cs._signature("GET", path, qs), timeout=20)
    if r is None or r.status_code >= 400:
        break
    b = r.json()
    for p in (b.get("data") or []):
        items.append((str(p.get("sellerProductId")), p.get("statusName") or "?"))
    nt = b.get("nextToken") or ""; page += 1
    if page % 100 == 0:
        print(f"  ...page {page}, {len(items)}", flush=True)
    if not nt or page > 900:
        break
    time.sleep(0.22)
print(f"total seller products: {len(items)}")

# 저장
with open("/home/ubuntu/logs/all_spids.txt", "w") as f:
    for spid, st in items:
        f.write(f"{spid}\t{st}\n")

# orphan 교차: 우리 listings_pa 에 없는 spid
con = sqlite3.connect("backend/purchase/purchase.db"); con.row_factory = sqlite3.Row
con.execute("PRAGMA busy_timeout=180000")
known = set(str(r["channel_product_id"]) for r in con.execute(
    "SELECT DISTINCT channel_product_id FROM listings_pa WHERE channel='coupang' AND channel_product_id IS NOT NULL").fetchall())
orphan = [(s, st) for s, st in items if s not in known]
by_st = collections.Counter(st for _, st in orphan)
print(f"\n=== 우리 listings_pa 에 spid 등록된 것: {len(known)} ===")
print(f"=== 쿠팡엔 있는데 우리 DB 에 없는 orphan: {len(orphan)} ===")
for st, c in by_st.most_common():
    print(f"   {st}: {c}")
with open("/home/ubuntu/logs/orphan_spids.txt", "w") as f:
    for s, st in orphan:
        f.write(f"{s}\t{st}\n")
print("저장: /home/ubuntu/logs/all_spids.txt, orphan_spids.txt")
