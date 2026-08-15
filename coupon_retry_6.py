"""6월 쿠폰 발급 429 재시도 — 50-70K 제외 6개 band, sleep 5초."""
import sys, os, time
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")

from backend.purchase.scripts.coupang_publish_coupon_policy import (
    COUPONS, create_coupon, discover_contract_id,
)
from datetime import datetime, timedelta, timezone

# 50-70K 는 이미 발행 (couponId=92761018) → 제외
SKIP_BANDS = {"50-70K"}

contract_id = discover_contract_id()
print(f"contract_id={contract_id}")

# 기간 — 6월 1일 00:00 KST ~ 6월 30일 23:59 KST
kst = timezone(timedelta(hours=9))
start_at = "2026-06-02 00:00:00"
end_at = "2026-06-30 23:59:00"
print(f"기간: {start_at} ~ {end_at}")

ok = fail = 0
for cp in COUPONS:
    band = cp.get("margin_band")
    if band in SKIP_BANDS:
        print(f"  [{band}] skip (이미 발행)")
        continue
    print(f"\n=== [{band}] 발급 시작 ===")
    try:
        coupon_id = create_coupon(cp, contract_id, start_at, end_at)
        if coupon_id:
            print(f"  ✓ couponId={coupon_id}")
            ok += 1
        else:
            fail += 1
    except Exception as e:
        fail += 1
        print(f"  ERR {e}")
    time.sleep(5)

print(f"\n=== 결과 ok {ok} / fail {fail} ===")
