"""G3 PUT OK 92건 검증 — statusName 분포 + vendorItemId 유지."""
import sys, os, time
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.services.coupang_service import get_seller_product

SPIDS = open("/tmp/g3_put_ok_spids.txt").read().split()
ok = fail = 0
status_ct = {}
vid_kept = vid_lost = 0
for s in SPIDS:
    info = get_seller_product(s)
    if not info or not isinstance(info.get("data"), dict):
        fail += 1
        continue
    d = info["data"]
    st = d.get("statusName") or "?"
    status_ct[st] = status_ct.get(st, 0) + 1
    items = d.get("items") or []
    if items and all(it.get("vendorItemId") for it in items):
        vid_kept += 1
    else:
        vid_lost += 1
    ok += 1
    time.sleep(0.2)

print(f"검증 ok={ok} fail={fail}")
print(f"statusName: {status_ct}")
print(f"vendorItemId 전수 유지: {vid_kept}  / 일부 None: {vid_lost}")
