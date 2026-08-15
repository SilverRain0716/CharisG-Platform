"""v2 정책 기준 audit — clean_axis_value 적용 후 옵션 분류.

분류:
  - all_drop: 모든 토큰이 cleaned None → 옵션 자체 의미 없음 → stop 대상
  - partial:  일부 토큰만 None → cleaned 후 살림 가능 (라벨 PUT 필요)
  - clean:    이미 깨끗
"""
import sys, os, sqlite3
from datetime import datetime
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.services.variation import clean_axis_value

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
OUT = f"/home/ubuntu/logs/noise_v2_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

def log(m=""):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {m}" if m else ""
    print(line, flush=True)
    with open(OUT, "a") as f:
        f.write(line + "\n")

conn = sqlite3.connect(DB, timeout=180)
conn.execute("PRAGMA busy_timeout=180000")
conn.row_factory = sqlite3.Row

log("=" * 70)
log(f"v2 audit 시작, 결과파일={OUT}")
log("=" * 70)

rows = conn.execute("""
  SELECT lo.id lo_id, lo.option_label, p.asin, p.brand, l.channel_product_id spid
  FROM listing_options lo
  JOIN listings_pa l ON l.id=lo.listing_id
  JOIN products p ON p.id=lo.child_product_id
  WHERE l.channel='coupang' AND l.status='listed' AND lo.status='active'
""").fetchall()
total = len(rows)
log(f"\n[대상] coupang listed + lo.active: {total:,}")

all_drop = []
partial = []
clean = 0
for r in rows:
    label = r["option_label"] or ""
    tokens = [t.strip() for t in label.split("/") if t.strip()]
    if not tokens:
        all_drop.append((r, label, []))
        continue
    cleaned_list = [(t, clean_axis_value(t)) for t in tokens]
    n_none = sum(1 for _, c in cleaned_list if c is None)
    n_kept = sum(1 for _, c in cleaned_list if c is not None)
    if n_kept == 0:
        all_drop.append((r, label, cleaned_list))
    elif n_none == 0:
        clean += 1
    else:
        partial.append((r, label, cleaned_list))

log(f"\n[분류 결과]")
log(f"  clean (변경 불필요):         {clean:,} ({100*clean/total:.1f}%)")
log(f"  partial (일부 노이즈 토큰):  {len(partial):,} ({100*len(partial)/total:.1f}%)  → 라벨 PUT 또는 보류")
log(f"  all_drop (전체 노이즈):      {len(all_drop):,} ({100*len(all_drop)/total:.1f}%)  → stop_sales 대상")

# spid 영향
all_drop_spids = {r["spid"] for r, _, _ in all_drop if r["spid"]}
partial_spids = {r["spid"] for r, _, _ in partial if r["spid"]}
log(f"\n[영향받은 sellerProduct]")
log(f"  all_drop:  {len(all_drop_spids):,}")
log(f"  partial:   {len(partial_spids):,}")

log("\n[샘플 all_drop 15건]")
for r, label, cl in all_drop[:15]:
    log(f"  spid={r['spid']} asin={r['asin']} '{label}'  cleaned={[c for _,c in cl]}")

log("\n[샘플 partial 15건 — 살림 가능]")
for r, label, cl in partial[:15]:
    new_label = " / ".join(c for _, c in cl if c is not None)
    log(f"  spid={r['spid']} asin={r['asin']} '{label}'")
    log(f"    → cleaned 후: '{new_label}'")

conn.close()
log("\n" + "=" * 70)
log("완료")
