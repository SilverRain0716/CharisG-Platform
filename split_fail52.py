"""검증 시점 조회실패 52건 영구/일시 분리 + 영구는 listings_pa.status 정리."""
import sys, os, time
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.services.coupang_service import get_seller_product
from backend.purchase.database import get_db

SPIDS = open("/tmp/g3_put_ok_spids.txt").read().split()
print(f"총 {len(SPIDS)} spids — 천천히 재검증 (sleep 1)")

ok = perma_fail = 0
status_ct = {}
perma = []
for s in SPIDS:
    info = get_seller_product(s)
    if info and isinstance(info.get("data"), dict):
        ok += 1
        st = info["data"].get("statusName") or "?"
        status_ct[st] = status_ct.get(st, 0) + 1
    else:
        perma_fail += 1
        perma.append(s)
    time.sleep(1)

print(f"\n검증 ok={ok} perma_fail={perma_fail}")
print(f"statusName: {status_ct}")
print(f"\n영구 fail spids ({len(perma)}):")
for s in perma:
    print(f"  {s}")

# 영구 fail 의 listings_pa.status 'listed' → 'removed' 정합성
if perma:
    with get_db() as conn:
        marker = ",".join(["?"]*len(perma))
        cur = conn.execute(
            f"UPDATE listings_pa SET status='removed', "
            f"error_message='쿠팡 GET 영구 실패 — sellerProduct 없음 (2026-05-31)', "
            f"last_synced_at=CURRENT_TIMESTAMP "
            f"WHERE channel='coupang' AND channel_product_id IN ({marker}) AND status='listed'",
            perma,
        )
        n = cur.rowcount
        conn.commit()
    print(f"\nlistings_pa.status 'listed' → 'removed' UPDATE: {n}건")
