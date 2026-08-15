"""v2 정책 all_drop 80건 → vendor-item stop_sales.

흐름:
  1) DB: clean_axis_value 로 all_drop 옵션 식별 (asin + spid)
  2) sellerProduct GET → items 의 externalVendorSku 로 vendor-item 매칭
  3) stop_sales_vendor_item 호출
"""
import sys, os, sqlite3, time, argparse
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.services.variation import clean_axis_value
from backend.purchase.services.coupang_service import get_seller_product, stop_sales_vendor_item

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--spid", default=None)
ap.add_argument("--sleep", type=float, default=0.5)
args = ap.parse_args()

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
conn = sqlite3.connect(DB, timeout=180)
conn.execute("PRAGMA busy_timeout=180000")
conn.row_factory = sqlite3.Row

# all_drop 추출
rows = conn.execute("""
  SELECT lo.option_label, p.asin, l.channel_product_id spid
  FROM listing_options lo
  JOIN listings_pa l ON l.id=lo.listing_id
  JOIN products p ON p.id=lo.child_product_id
  WHERE l.channel='coupang' AND l.status='listed' AND lo.status='active'
    AND l.channel_product_id IS NOT NULL AND l.channel_product_id != ''
""").fetchall()

# all_drop: 모든 토큰 cleaned None
targets = {}  # spid -> set of ASIN
for r in rows:
    tokens = [t.strip() for t in (r["option_label"] or "").split("/") if t.strip()]
    if not tokens:
        continue
    if all(clean_axis_value(t) is None for t in tokens):
        targets.setdefault(r["spid"], set()).add(r["asin"])

if args.spid:
    targets = {args.spid: targets.get(args.spid, set())}
if args.limit > 0:
    targets = dict(list(targets.items())[:args.limit])

total_asins = sum(len(v) for v in targets.values())
print(f"[대상] {len(targets)} sellerProduct, {total_asins} ASIN (옵션)  apply={args.apply}")

stop_ok = stop_fail = no_vid = 0
for spid, asin_set in targets.items():
    info = get_seller_product(str(spid))
    if not info or not isinstance(info.get("data"), dict):
        print(f"  FAIL spid={spid}: 조회 실패")
        stop_fail += len(asin_set)
        time.sleep(args.sleep)
        continue
    items = info["data"].get("items") or []
    asin_to_vid = {it.get("externalVendorSku"): it.get("vendorItemId")
                   for it in items if it.get("externalVendorSku")}
    print(f"  spid={spid}: items {len(items)}, target asins {len(asin_set)}")
    for asin in sorted(asin_set):
        vid = asin_to_vid.get(asin)
        if not vid:
            no_vid += 1
            print(f"    NO_VID asin={asin}")
            continue
        if not args.apply:
            stop_ok += 1
            print(f"    DRY asin={asin} vid={vid}")
            continue
        success, msg = stop_sales_vendor_item(str(vid))
        if success:
            stop_ok += 1
            print(f"    STOP OK asin={asin} vid={vid}")
        else:
            stop_fail += 1
            print(f"    STOP FAIL asin={asin} vid={vid}: {msg[:80]}")
        time.sleep(0.4)
    time.sleep(args.sleep)

print(f"\n[총: stop ok {stop_ok} / fail {stop_fail} / no-vid {no_vid}]")
conn.close()
