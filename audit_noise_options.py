"""기존 listing_options 노이즈 audit.

is_noisy_axis_value 로 option_label 의 각 slash 단위 평가 → 분류:
  - active+listed 만 대상 (paused/removed 는 무관)
  - noise 라벨 갯수 + 영향받은 listing_id 갯수 + 샘플 100건
"""
import sys, os, sqlite3
from datetime import datetime
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.services.variation import is_noisy_axis_value

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
OUT = f"/home/ubuntu/logs/noise_options_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
os.makedirs("/home/ubuntu/logs", exist_ok=True)

def log(m=""):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {m}" if m else ""
    print(line, flush=True)
    with open(OUT, "a") as f:
        f.write(line + "\n")

conn = sqlite3.connect(DB, timeout=180)
conn.execute("PRAGMA busy_timeout=180000")
conn.row_factory = sqlite3.Row

log("=" * 70)
log(f"노이즈 옵션 audit 시작, 결과파일={OUT}")
log("=" * 70)

# active listed 옵션만
rows = conn.execute("""
  SELECT lo.id, lo.listing_id, lo.option_label, lo.channel_option_id, lo.status,
         p.asin, p.brand, l.channel_product_id as spid, l.status as listing_status
  FROM listing_options lo
  JOIN listings_pa l ON l.id=lo.listing_id
  JOIN products p ON p.id=lo.child_product_id
  WHERE l.channel='coupang' AND l.status='listed' AND lo.status='active'
""").fetchall()
total = len(rows)
log(f"\n[대상] coupang listed + lo.active: {total:,}건")

noisy = []
for r in rows:
    label = r["option_label"] or ""
    # option_label = "사이즈 / 색상 / ..." 처럼 slash 분리. 각 토큰 별 검사.
    tokens = [t.strip() for t in label.split("/") if t.strip()]
    bad_tokens = [t for t in tokens if is_noisy_axis_value(t)]
    if bad_tokens:
        noisy.append((r, bad_tokens))

log(f"\n[결과] 노이즈 옵션 {len(noisy):,}건 ({100*len(noisy)/total:.1f}%)")

# 영향받은 listing_id (sellerProductId) 갯수
spids = {r["spid"] for r, _ in noisy if r["spid"]}
log(f"[영향받은 sellerProduct] {len(spids):,}건 (= 라이브 sellerProductId 단위)")

# 패턴별 카운트
import re
pat_counts = {"+": 0, "Pack of": 0, "N Pack": 0, "카테고리 단어": 0, "60자초과": 0}
for r, bad in noisy:
    for t in bad:
        if "+" in t: pat_counts["+"] += 1
        if re.search(r"Pack of \d+", t, re.IGNORECASE): pat_counts["Pack of"] += 1
        if re.search(r"\d+\s*Pack", t, re.IGNORECASE): pat_counts["N Pack"] += 1
        if re.search(r"snorkeling|diving|swimming|scavenger|drinkware", t, re.IGNORECASE):
            pat_counts["카테고리 단어"] += 1
        if len(t) > 60: pat_counts["60자초과"] += 1
log("[패턴 카운트]")
for k, v in pat_counts.items():
    log(f"  {k}: {v:,}")

log(f"\n[샘플 30건]")
for r, bad in noisy[:30]:
    log(f"  spid={r['spid']} asin={r['asin']} ('{r['option_label'][:70]}') noisy={bad[:3]}")

conn.close()
log("\n" + "=" * 70)
log("audit 완료")
