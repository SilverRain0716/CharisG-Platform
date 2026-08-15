"""variation_groups false-positive 조사 — children=0 row 의 분포/원인/영향 측정.

PC OFF 환경 대비 nohup 으로 백그라운드 실행 + /home/ubuntu/logs/ 에 영구 로그.
"""
import os, sys, sqlite3, json
from datetime import datetime

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
OUT = f"/home/ubuntu/logs/variation_groups_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
os.makedirs("/home/ubuntu/logs", exist_ok=True)

def log(msg=""):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}" if msg else ""
    print(line, flush=True)
    with open(OUT, "a") as f:
        f.write(line + "\n")

conn = sqlite3.connect(DB, timeout=300)
conn.execute("PRAGMA busy_timeout=180000")
conn.row_factory = sqlite3.Row

log("=" * 70)
log(f"variation_groups false-positive 조사 시작 — DB={DB}")
log(f"로그파일={OUT}")
log("=" * 70)

# 1. 스키마
cols = conn.execute("PRAGMA table_info(variation_groups)").fetchall()
log(f"\n[1] variation_groups 스키마 ({len(cols)} cols)")
for c in cols:
    log(f"    {c['name']:<25} {c['type']}")
col_names = {c['name'] for c in cols}

# 2. 카운트
log("\n[2] 카운트")
total = conn.execute("SELECT COUNT(*) FROM variation_groups").fetchone()[0]
log(f"    전체 variation_groups: {total:,}")

no_child = conn.execute("""
    SELECT COUNT(*) FROM variation_groups vg
    WHERE NOT EXISTS (SELECT 1 FROM products p WHERE p.parent_asin = vg.parent_asin)
""").fetchone()[0]
log(f"    자식 0건 (false-positive): {no_child:,}  ({100*no_child/total:.1f}%)")

one_child = conn.execute("""
    SELECT COUNT(*) FROM variation_groups vg
    WHERE 1 = (SELECT COUNT(*) FROM products p WHERE p.parent_asin = vg.parent_asin)
""").fetchone()[0]
log(f"    자식 1건 only: {one_child:,}")

gte2 = total - no_child - one_child
log(f"    자식 ≥ 2 (정상 그룹): {gte2:,}")

# 3. 시간 분포 (created_at 또는 last_resync_at 이 있으면)
time_col = None
for c in ("created_at", "last_resync_at", "synced_at", "last_synced_at"):
    if c in col_names:
        time_col = c
        break

if time_col:
    log(f"\n[3] 월별 false-positive 분포 ({time_col} 기준)")
    rows = conn.execute(f"""
        SELECT substr({time_col},1,7) as ym, COUNT(*) as n
        FROM variation_groups vg
        WHERE NOT EXISTS (SELECT 1 FROM products p WHERE p.parent_asin = vg.parent_asin)
        GROUP BY ym ORDER BY ym
    """).fetchall()
    for r in rows:
        log(f"    {r['ym']}: {r['n']:,}")
else:
    log("\n[3] 시간 컬럼 없음 — 분포 측정 skip")

# 4. queue 영향
log("\n[4] group_registration_queue 의 false-positive 노출")
queue_total = conn.execute("SELECT COUNT(*) FROM group_registration_queue").fetchone()[0]
log(f"    queue 전체: {queue_total:,}")

fp_in_queue = conn.execute("""
    SELECT COUNT(*) FROM group_registration_queue q
    WHERE q.parent_asin IN (
      SELECT vg.parent_asin FROM variation_groups vg
      WHERE NOT EXISTS (SELECT 1 FROM products p WHERE p.parent_asin = vg.parent_asin)
    )
""").fetchone()[0]
log(f"    false-positive parent 가 큐에 들어온 적: {fp_in_queue:,}")

# 5. queue.skip_reason 별 false-positive 분포
log("\n[5] false-positive 가 큐에서 어떻게 처리됐나 (status × reason)")
fp_status = conn.execute("""
    SELECT q.status, substr(q.skip_reason,1,60) as reason, COUNT(*) n
    FROM group_registration_queue q
    WHERE q.parent_asin IN (
      SELECT vg.parent_asin FROM variation_groups vg
      WHERE NOT EXISTS (SELECT 1 FROM products p WHERE p.parent_asin = vg.parent_asin)
    )
    GROUP BY q.status, reason ORDER BY n DESC LIMIT 20
""").fetchall()
for r in fp_status:
    log(f"    {r['status']:<12} n={r['n']:<6} reason={r['reason']!r}")

# 6. 샘플 (가장 최근 추정)
log("\n[6] false-positive 샘플 5건")
order_by = f"ORDER BY vg.{time_col} DESC" if time_col else ""
samples = conn.execute(f"""
    SELECT vg.* FROM variation_groups vg
    WHERE NOT EXISTS (SELECT 1 FROM products p WHERE p.parent_asin = vg.parent_asin)
    {order_by} LIMIT 5
""").fetchall()
for s in samples:
    d = dict(s)
    short = {k: (v[:60] + "…" if isinstance(v, str) and len(v) > 60 else v)
             for k, v in d.items()}
    log(f"    {short}")

# 7. variation_groups 의 children_count 또는 children_json 검증 — 내부 메타가 실제 products 와 일치하는지
if "children_json" in col_names:
    log("\n[7] children_json 내부 vs 실제 products 자식 불일치 (5건 샘플)")
    mismatch = conn.execute("""
        SELECT vg.parent_asin, vg.children_json
        FROM variation_groups vg
        WHERE NOT EXISTS (SELECT 1 FROM products p WHERE p.parent_asin = vg.parent_asin)
          AND vg.children_json IS NOT NULL AND vg.children_json != '[]'
        LIMIT 5
    """).fetchall()
    for m in mismatch:
        try:
            kids = json.loads(m["children_json"])
            log(f"    {m['parent_asin']}: children_json 안에 {len(kids)} ASIN — 그러나 products 에 1건도 없음")
            log(f"      샘플 ASIN: {kids[:3] if kids else []}")
        except Exception as e:
            log(f"    {m['parent_asin']}: parse 실패 {e}")

# 8. 정합성 검사 — children_json 의 ASIN 이 products 에 있는지
log("\n[8] children_json 내 ASIN 의 products 존재 비율")
if "children_json" in col_names:
    sample_rows = conn.execute("""
        SELECT parent_asin, children_json FROM variation_groups
        WHERE children_json IS NOT NULL AND children_json != '[]'
        ORDER BY ROWID DESC LIMIT 200
    """).fetchall()
    asin_present = asin_missing = 0
    for r in sample_rows:
        try:
            kids = json.loads(r["children_json"]) or []
        except Exception:
            continue
        for a in kids[:20]:
            cur = conn.execute("SELECT 1 FROM products WHERE asin=? LIMIT 1", (a,)).fetchone()
            if cur:
                asin_present += 1
            else:
                asin_missing += 1
    tot = asin_present + asin_missing
    if tot:
        log(f"    sampled rows=200, asin checked={tot:,}  present={asin_present:,} ({100*asin_present/tot:.1f}%) / missing={asin_missing:,}")

# 9. 최근 7일 false-positive 가속도
if time_col:
    log("\n[9] 최근 7일/30일 false-positive 증가 추이")
    for span in (7, 30):
        n = conn.execute(f"""
            SELECT COUNT(*) FROM variation_groups vg
            WHERE NOT EXISTS (SELECT 1 FROM products p WHERE p.parent_asin = vg.parent_asin)
              AND {time_col} >= datetime('now', '-{span} days')
        """).fetchone()[0]
        log(f"    최근 {span}일: {n:,}")

log("\n" + "=" * 70)
log("조사 완료")
log("=" * 70)
conn.close()
