#!/bin/bash
# 4시간 후 16233785543 (Aleefa 도수 마스크) 자동 stop_sales
sleep 14400
cd /home/ubuntu/CharisG-Platform/charisg-platform
source .venv/bin/activate
python3 -c "
import os, sys, time
os.chdir('/home/ubuntu/CharisG-Platform/charisg-platform')
sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv('.env')
from backend.purchase.services.coupang_service import get_seller_product, stop_sales_vendor_item
info = get_seller_product('16233785543')
if not info or not isinstance(info.get('data'), dict):
    print('조회 실패'); exit(1)
d = info['data']
items = d.get('items') or []
print(f'statusName={d.get(\"statusName\")!r} items={len(items)}')
ok = fail = no_vid = 0
for it in items:
    vid = it.get('vendorItemId')
    if not vid:
        no_vid += 1; continue
    success, msg = stop_sales_vendor_item(str(vid))
    if success: ok += 1; print(f'  STOP OK vid={vid}')
    else: fail += 1; print(f'  STOP FAIL vid={vid}: {msg[:80]}')
    time.sleep(0.5)
print(f'총 stop ok={ok} fail={fail} no_vid={no_vid}')
"
