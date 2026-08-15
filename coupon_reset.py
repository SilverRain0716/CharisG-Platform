"""쿠폰 정리 + 새로 7개 발급 + 즉시 catchup.

1) DB 의 기존 06월 쿠폰 9개 status='expired' (안 씀)
2) 7개 새 쿠폰 발급 (start_at=즉시, end_at=6/30)
3) 별도 catchup 호출
"""
import sys, os, sqlite3, time
from datetime import datetime, timedelta, timezone
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.services.coupang_service import (
    create_coupon, discover_contract_id, wait_for_request,
)

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"

# 7단계 정책 (band, suffix, type, discount, cap, lo, hi)
COUPONS = [
    ("10-15K",   "마진10-15K_정액1000",   "PRICE",  1000, 10000,  10000,  15000),
    ("15-50K",   "마진15-50K_5pct_cap10K", "RATE",     5, 10000,  15000,  50000),
    ("50-70K",   "마진50-70K_정액5000",   "PRICE",  5000, 10000,  50000,  70000),
    ("70-100K",  "마진70-100K_정액10000", "PRICE", 10000, 20000,  70000, 100000),
    ("100-150K", "마진100-150K_정액20000","PRICE", 20000, 30000, 100000, 150000),
    ("150-200K", "마진150-200K_정액30000","PRICE", 30000, 50000, 150000, 200000),
    ("200K+",    "마진200K+_정액50000",   "PRICE", 50000, 70000, 200000,  10**12),
]

# 1) DB 옛 쿠폰 정리
print("=== 1) 옛 쿠폰 expired 마킹 ===")
conn = sqlite3.connect(DB, timeout=60)
cur = conn.execute(
    "UPDATE coupons SET status='expired' WHERE coupon_id IN ('92761016','92761017','92761018','92761481','92761483','92761484','92761486','92761488','92761489')"
)
print(f"  {cur.rowcount}개 → expired")
conn.commit()

# 2) 새 7개 발급 (지금 시각 + 1분 = 즉시, end_at = 6/30 23:59 KST)
contract_id = discover_contract_id()
kst = timezone(timedelta(hours=9))
now_kst = datetime.now(kst)
# 쿠팡 start_at 은 미래 시각 필요 (분 단위). 안전 마진 30분 더해 시작.
start_kst = (now_kst + timedelta(minutes=30)).replace(second=0, microsecond=0)
start_at = start_kst.strftime("%Y-%m-%d %H:%M:%S")
end_at = "2026-06-30 23:59:00"
print(f"\n=== 2) 새 7개 발급 ({start_at} ~ {end_at}, contract={contract_id}) ===")
ok = fail = 0
for band, suffix, t_, disc, cap, lo, hi in COUPONS:
    name = f"CharisG_{suffix}_2606new"
    print(f"\n  [{band}] {name}")
    try:
        success, err, req_id = create_coupon(
            contract_id=contract_id, name=name,
            discount=disc, max_discount_price=cap,
            start_at=start_at, end_at=end_at, type_=t_,
        )
        if not success:
            fail += 1; print(f"    ERR {err}"); time.sleep(6); continue
        result = wait_for_request(req_id, timeout=120, interval=2)
        if not result or result.get("status") != "DONE":
            fail += 1; print(f"    polling FAIL: {result}"); time.sleep(6); continue
        cid = result.get("couponId")
        ok += 1; print(f"    ✓ couponId={cid}")
        conn.execute(
            """INSERT INTO coupons (contract_id, name, type, discount, max_discount_price,
                                    start_at, end_at, status, coupon_id, requested_id, margin_band)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
            (contract_id, name, t_, disc, cap, start_at, end_at,
             str(cid), req_id, band),
        )
        conn.commit()
    except Exception as e:
        fail += 1; print(f"    EX {e}")
    time.sleep(6)

print(f"\n=== 결과 ok {ok} / fail {fail} ===")
conn.close()
