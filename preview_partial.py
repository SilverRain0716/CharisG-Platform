"""partial 옵션 cleaning 미리보기 — sellerProduct 별 group + 변경 라벨 표.

read-only. (G3) 실 PUT 전 검토용.
"""
import sys, os, sqlite3
from collections import defaultdict
from datetime import datetime
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.services.variation import clean_axis_value

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
OUT = f"/home/ubuntu/logs/partial_preview_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

def log(m=""):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {m}" if m else ""
    print(line, flush=True)
    with open(OUT, "a") as f:
        f.write(line + "\n")

conn = sqlite3.connect(DB, timeout=180)
conn.row_factory = sqlite3.Row

log(f"partial preview 시작, 결과파일={OUT}")

rows = conn.execute("""
  SELECT lo.id lo_id, lo.option_label, p.asin, l.channel_product_id spid
  FROM listing_options lo
  JOIN listings_pa l ON l.id=lo.listing_id
  JOIN products p ON p.id=lo.child_product_id
  WHERE l.channel='coupang' AND l.status='listed' AND lo.status='active'
    AND l.channel_product_id IS NOT NULL AND l.channel_product_id != ''
""").fetchall()

by_spid = defaultdict(list)
for r in rows:
    label = r["option_label"] or ""
    tokens = [t.strip() for t in label.split("/") if t.strip()]
    if not tokens:
        continue
    cleaned = [clean_axis_value(t) for t in tokens]
    n_kept = sum(1 for c in cleaned if c is not None)
    n_none = sum(1 for c in cleaned if c is None)
    if n_kept > 0 and (n_none > 0 or any(c != t for c, t in zip(cleaned, tokens) if c)):
        # dedupe — 중복 토큰 제거 (옵션 중복 거부 방지)
        seen = set()
        unique = []
        for c in cleaned:
            if c is None:
                continue
            if c not in seen:
                seen.add(c)
                unique.append(c)
        new_label = " / ".join(unique)
        if new_label != label:
            by_spid[r["spid"]].append({
                "asin": r["asin"], "old": label, "new": new_label,
                "drop_count": n_none, "keep_count": n_kept,
            })

log(f"\n[partial sellerProduct 영향]")
log(f"  unique spids: {len(by_spid)}")
log(f"  total partial options: {sum(len(v) for v in by_spid.values())}")

log("\n[sellerProduct 별 상위 12개 — 옵션 갯수 많은 순]")
sorted_spids = sorted(by_spid.items(), key=lambda x: -len(x[1]))
for spid, opts in sorted_spids[:12]:
    log(f"\n  spid={spid}  partial옵션 {len(opts)}개")
    for o in opts[:5]:
        log(f"    {o['asin']}: '{o['old'][:55]}'")
        log(f"      → '{o['new'][:55]}'")
    if len(opts) > 5:
        log(f"    ... +{len(opts)-5} 건 더")

log(f"\n[Top 영향 받는 sellerProduct]")
for spid, opts in sorted_spids[:25]:
    log(f"  spid={spid}: {len(opts)}건")

conn.close()
log("\n완료")
