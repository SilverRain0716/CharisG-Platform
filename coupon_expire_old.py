"""쿠팡 측 옛 쿠폰 expire — 새 7개 (92761648~92761657) 외 모두 정지."""
import sys, os, time, sqlite3
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.services.coupang_service import expire_coupon

# 우리 의도 7개 (남길 것)
KEEP = {"92761648", "92761651", "92761652", "92761654", "92761655", "92761656", "92761657"}

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
conn = sqlite3.connect(DB, timeout=60)
conn.row_factory = sqlite3.Row

# DB 의 모든 6월 쿠폰 (06월 시작) — KEEP 제외 후 expire 시도
rows = conn.execute(
    "SELECT coupon_id, margin_band, name FROM coupons "
    "WHERE start_at >= '2026-06-01' AND coupon_id NOT IN ('92761648','92761651','92761652','92761654','92761655','92761656','92761657') "
    "ORDER BY coupon_id"
).fetchall()

# 그리고 92722982 (스크린샷에서 발견 — 알 수 없는 10-15K)
EXTRA = ["92722982"]
target_ids = [r["coupon_id"] for r in rows] + EXTRA

print(f"=== expire 대상 {len(target_ids)} 쿠폰 ===")
ok = fail = 0
for cid in target_ids:
    print(f"  expire {cid}", end=" ")
    try:
        success, err, req_id = expire_coupon(int(cid))
        if success:
            ok += 1
            print(f"OK reqId={req_id}")
            conn.execute("UPDATE coupons SET status='expired' WHERE coupon_id=?", (cid,))
            conn.commit()
        else:
            fail += 1
            print(f"FAIL: {err[:80]}")
    except Exception as e:
        fail += 1
        print(f"EX {e}")
    time.sleep(3)

print(f"\n=== 결과 ok {ok} / fail {fail} ===")
conn.close()
