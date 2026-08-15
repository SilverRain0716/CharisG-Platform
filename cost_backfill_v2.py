"""cost backfill v2 — 그룹 + 단독 ASIN 모두 처리.

1) assign_cost_via_pricing 시도 (variation_groups 있는 케이스)
2) 'variation_groups 없음' 이면 _get_buybox_or_lowest_price 직접 호출 (단독 ASIN)
3) 결과 cost_usd UPDATE
"""
import sys, os, sqlite3, time
from datetime import datetime
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.services.group_lister import (
    assign_cost_via_pricing, _get_buybox_or_lowest_price,
)

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
OUT = f"/home/ubuntu/logs/cost_backfill_v2_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

def log(m=""):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {m}" if m else ""
    print(line, flush=True)
    with open(OUT, "a") as f:
        f.write(line + "\n")

conn = sqlite3.connect(DB, timeout=180)
conn.execute("PRAGMA busy_timeout=180000")
conn.row_factory = sqlite3.Row

PARENTS = [r[0] for r in conn.execute(
    "SELECT DISTINCT parent_asin FROM group_registration_queue WHERE status='queued'"
).fetchall()]
conn.close()

log(f"=== v2 cost backfill 시작 — {len(PARENTS):,} parent_asin ===")
log(f"=== 결과 {OUT} ===")

grp_ok = single_ok = single_no_data = fail = 0
for i, asin in enumerate(PARENTS, 1):
    try:
        r = assign_cost_via_pricing(asin, fallback_master_cost=True)
        if r.get("error") == "variation_groups 없음":
            # 단독 ASIN — products 행에 직접 cost 채움
            price = _get_buybox_or_lowest_price(asin)
            if price and price > 0:
                cn = sqlite3.connect(DB, timeout=60)
                cn.execute("PRAGMA busy_timeout=60000")
                cn.execute(
                    "UPDATE products SET cost_usd=? WHERE asin=? AND (cost_usd IS NULL OR cost_usd<=0)",
                    (float(price), asin),
                )
                cn.commit()
                cn.close()
                single_ok += 1
            else:
                single_no_data += 1
        else:
            grp_ok += 1
    except Exception as e:
        fail += 1
        log(f"  [{i}] FAIL {asin}: {str(e)[:80]}")
    if i % 50 == 0:
        log(f"  [{i}/{len(PARENTS)}] grp={grp_ok} single_ok={single_ok} no_data={single_no_data} fail={fail}")
    time.sleep(0.5)

log(f"\n=== 완료 grp={grp_ok} single_ok={single_ok} no_data={single_no_data} fail={fail} ===")
