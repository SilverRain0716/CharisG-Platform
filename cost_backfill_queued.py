"""queued parent_asin 의 cost 백필 — SP-API Pricing + master_cost fallback.

각 parent_asin 에 대해 assign_cost_via_pricing 호출 → children cost_usd UPDATE.
워커가 처리하기 전에 cost 채워 페이로드 생성 실패 막음.
"""
import sys, os, sqlite3, time
from datetime import datetime
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.services.group_lister import assign_cost_via_pricing

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
OUT = f"/home/ubuntu/logs/cost_backfill_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

def log(m=""):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {m}" if m else ""
    print(line, flush=True)
    with open(OUT, "a") as f:
        f.write(line + "\n")

conn = sqlite3.connect(DB, timeout=180)
conn.row_factory = sqlite3.Row

# queued parent 중 children 에 cost 없는 게 있는 것만
rows = conn.execute("""
  SELECT DISTINCT q.parent_asin
  FROM group_registration_queue q
  WHERE q.status='queued'
""").fetchall()
conn.close()

PARENTS = [r["parent_asin"] for r in rows]
log(f"=== cost backfill 시작 — {len(PARENTS):,} parent_asin ===")
log(f"=== 결과파일 {OUT} ===")

ok = fail = 0
for i, asin in enumerate(PARENTS, 1):
    try:
        result = assign_cost_via_pricing(asin, fallback_master_cost=True)
        ok += 1
        if i % 50 == 0:
            log(f"  [{i}/{len(PARENTS)}] ok={ok} fail={fail}  {asin}: {str(result)[:80]}")
    except Exception as e:
        fail += 1
        log(f"  [{i}/{len(PARENTS)}] FAIL {asin}: {str(e)[:80]}")
    time.sleep(0.5)  # SP-API rate

log(f"\n=== 완료 ok={ok} fail={fail} ===")
