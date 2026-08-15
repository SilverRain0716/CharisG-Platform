"""6월 쿠폰 발급 retry — 50-70K 제외 6개 band, sleep 5초 (rate 회피)."""
import sys, os, time, sqlite3
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.services.coupang_service import (
    create_coupon, discover_contract_id, wait_for_request,
)

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"

# (band, suffix, type, discount, cap, lo, hi)
COUPONS = [
    ("10-15K",   "마진10-15K_정액1000",   "PRICE",  1000, 10000,  10000,  15000),
    ("15-50K",   "마진15-50K_5pct_cap10K", "RATE",     5, 10000,  15000,  50000),
    # ("50-70K",   ...) ← 이미 발행 92761018
    ("70-100K",  "마진70-100K_정액10000", "PRICE", 10000, 20000,  70000, 100000),
    ("100-150K", "마진100-150K_정액20000","PRICE", 20000, 30000, 100000, 150000),
    ("150-200K", "마진150-200K_정액30000","PRICE", 30000, 50000, 150000, 200000),
    ("200K+",    "마진200K+_정액50000",   "PRICE", 50000, 70000, 200000,  10**12),
]

contract_id = discover_contract_id()
print(f"contract_id={contract_id}")
start_at = "2026-06-02 00:00:00"
end_at = "2026-06-30 23:59:00"
print(f"기간: {start_at} ~ {end_at}\n")

conn = sqlite3.connect(DB, timeout=180)
ok = fail = 0
for band, suffix, t_, disc, cap, lo, hi in COUPONS:
    coupon_name = f"CharisG_{suffix}_2606"
    print(f"=== [{band}] 발급: {coupon_name} ===")
    try:
        success, err, req_id = create_coupon(
            contract_id=contract_id, name=coupon_name,
            discount=disc, max_discount_price=cap,
            start_at=start_at, end_at=end_at, type_=t_,
        )
        if not success:
            fail += 1; print(f"  ERR {err}"); time.sleep(6); continue
        print(f"  create_coupon ok, reqId={req_id}")
        result = wait_for_request(req_id, timeout=120, interval=2)
        if not result or result.get("status") != "DONE":
            fail += 1; print(f"  polling FAIL: {result}"); time.sleep(6); continue
        coupon_id = result.get("couponId")
        ok += 1; print(f"  ✓ couponId={coupon_id}")
        # coupons 테이블 INSERT
        conn.execute(
            """INSERT INTO coupons (contract_id, name, type, discount, max_discount_price,
                                    start_at, end_at, status, coupon_id, requested_id, margin_band)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
            (contract_id, coupon_name, t_, disc, cap, start_at, end_at,
             str(coupon_id), req_id, band),
        )
        conn.commit()
    except Exception as e:
        fail += 1; print(f"  EX {e}")
    time.sleep(6)

print(f"\n=== 결과 ok {ok} / fail {fail} ===")
conn.close()
