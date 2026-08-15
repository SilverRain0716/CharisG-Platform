"""sid=16, sid=17 진단 — products status 분포 + listings + 정지 원인 추적."""
import sys, os, sqlite3
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
conn = sqlite3.connect(DB, timeout=180)
conn.row_factory = sqlite3.Row

# products 스키마에 sheet_id 가 있나?
cols = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
print(f"products cols 일부: sheet_id={'sheet_id' in cols} sheet_source_id={'sheet_source_id' in cols} sourcing_queue_id={'sourcing_queue_id' in cols}")
print(f"  주요 cols: {[c for c in cols if 'sheet' in c.lower() or 'sourc' in c.lower()][:10]}")

# sheet_queue 의 product 매핑이 어디 있는지
for sid in (16, 17):
    print(f"\n=== sid={sid} ===")
    sq = conn.execute("SELECT * FROM sheet_queue WHERE id=?", (sid,)).fetchone()
    if sq:
        for k in sq.keys():
            v = sq[k]
            if v is not None and v != "":
                print(f"  {k}: {str(v)[:80]}")

# sheet_queue 와 products 연결 — 어느 컬럼?
# 추정: products.sheet_queue_id 또는 별도 매핑 테이블
print("\n[연결 테이블 후보]")
tbls = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
for t in tbls:
    if "sheet" in t.lower() and t != "sheet_queue":
        print(f"  {t}")
        c2 = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
        print(f"    cols: {sorted(c2)[:15]}")

conn.close()
