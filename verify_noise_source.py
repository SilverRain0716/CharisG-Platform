"""노이즈 판정된 옵션 라벨이 진짜 SP-API 원본 값인지 검증.

각 noisy listing_options 행에 대해:
  - child_product_id → products.sp_api_facts_json 안 color/size/style/flavor 추출
  - option_label 분해 후 각 토큰이 SP-API 원본에 그대로 있는지 매칭
  - 결론: 'SP-API 원본 노이즈' vs '우리가 합쳐 만든 노이즈' 분류
"""
import sys, os, sqlite3, json, re
from datetime import datetime
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.services.variation import is_noisy_axis_value

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
OUT = f"/home/ubuntu/logs/noise_verify_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

def log(m=""):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {m}" if m else ""
    print(line, flush=True)
    with open(OUT, "a") as f:
        f.write(line + "\n")

conn = sqlite3.connect(DB, timeout=180)
conn.execute("PRAGMA busy_timeout=180000")
conn.row_factory = sqlite3.Row

log("=" * 70)
log(f"노이즈 진위 검증 시작, 결과파일={OUT}")
log("=" * 70)

# noisy 옵션 + child 의 sp_api_facts_json 가져오기
rows = conn.execute("""
  SELECT lo.id lo_id, lo.option_label, p.id pid, p.asin, p.brand,
         p.sp_api_facts_json, l.channel_product_id spid
  FROM listing_options lo
  JOIN listings_pa l ON l.id=lo.listing_id
  JOIN products p ON p.id=lo.child_product_id
  WHERE l.channel='coupang' AND l.status='listed' AND lo.status='active'
""").fetchall()

noisy_rows = []
for r in rows:
    label = r["option_label"] or ""
    tokens = [t.strip() for t in label.split("/") if t.strip()]
    bad = [t for t in tokens if is_noisy_axis_value(t)]
    if bad:
        noisy_rows.append((r, bad))

log(f"\n[대상] noisy {len(noisy_rows):,}건")

def extract_sp_attrs(facts_json):
    if not facts_json:
        return {}
    try:
        d = json.loads(facts_json)
    except Exception:
        return {}
    # 주요 axis 값 추출
    out = {}
    for k in ("color", "color_name", "size", "size_label", "style", "flavor", "flavor_attr",
              "pattern", "scent", "material", "model_number", "number_of_items"):
        v = d.get(k)
        if v not in (None, ""):
            out[k] = str(v)
    # variationTheme + variation_axes
    if d.get("variation_theme"):
        out["__theme__"] = d["variation_theme"]
    return out

# 검증 — token 별 source
real_count = made_count = 0
real_samples = []
made_samples = []

for r, bad in noisy_rows:
    sp = extract_sp_attrs(r["sp_api_facts_json"])
    sp_concat = " | ".join(f"{k}={v}" for k, v in sp.items())
    sp_lower_values = {v.lower() for v in sp.values()}

    for t in bad:
        # SP-API 의 어떤 axis 값과 일치하나? (case-insensitive substring + 동등)
        t_low = t.lower()
        match = None
        # 완전 일치
        for k, v in sp.items():
            if v and t_low == v.lower():
                match = ("=", k, v)
                break
        # substring
        if not match:
            for k, v in sp.items():
                if v and len(v) > 3 and (t_low in v.lower() or v.lower() in t_low):
                    match = ("~", k, v)
                    break

        if match:
            real_count += 1
            if len(real_samples) < 15:
                real_samples.append((r["asin"], t, match, sp_concat[:100]))
        else:
            made_count += 1
            if len(made_samples) < 25:
                made_samples.append((r["asin"], t, sp_concat[:100]))

log(f"\n[token 출처 분류]")
log(f"  SP-API 원본 일치 (진짜 product 값): {real_count:,}")
log(f"  SP-API 원본 미일치 (우리 시스템이 합성): {made_count:,}")

log(f"\n[샘플: SP-API 원본 노이즈 = 진짜 product 값]")
for a, t, m, sp in real_samples:
    log(f"  {a} '{t}' ← {m[0]} {m[1]}={m[2]!r}  SP-API({sp})")

log(f"\n[샘플: SP-API 미일치 = 우리 합성 노이즈]")
for a, t, sp in made_samples:
    log(f"  {a} '{t}' ← SP-API({sp})")

conn.close()
log("\n" + "=" * 70)
log("완료")
