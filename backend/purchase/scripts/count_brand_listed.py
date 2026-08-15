"""신규 차단 브랜드가 이미 listed 된 건수 집계 (read-only). 필터와 동일 매칭."""
import re, sqlite3
DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
BRANDS = ["New Balance","뉴발란스","Under Armour","언더아머","Nike","나이키","Logitech","로지텍",
          "Amos","아모스","IOPE","아이오페","JBL","제이비엘","Bose","Le Creuset","르크루제","Braun",
          "Starbucks","스타벅스","The Ordinary","디오디너리","Estee Lauder","Estée Lauder","에스티로더",
          "Pino","Cetaphil","세타필","Lancome","Lancôme","랑콤","Asics","아식스"]
# 표시용 그룹(영문 키 기준으로 한글/영문 합산)
GROUP = {"뉴발란스":"New Balance","언더아머":"Under Armour","나이키":"Nike","로지텍":"Logitech",
         "아모스":"Amos","아이오페":"IOPE","제이비엘":"JBL","르크루제":"Le Creuset","스타벅스":"Starbucks",
         "디오디너리":"The Ordinary","Estée Lauder":"Estee Lauder","에스티로더":"Estee Lauder",
         "세타필":"Cetaphil","Lancôme":"Lancome","랑콤":"Lancome","아식스":"Asics"}

def match(en, ko):
    raw = f"{ko or ''} {en or ''}"; up = raw.upper()
    for b in BRANDS:
        if re.search(r"[A-Za-z]", b):
            if re.search(rf"\b{re.escape(b.upper())}\b", up):
                return GROUP.get(b, b)
        elif b in raw:
            return GROUP.get(b, b)
    return None

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); c.row_factory = sqlite3.Row
rows = c.execute("SELECT p.title_en, p.title_ko FROM listings_pa l JOIN products p ON p.id=l.product_id "
                 "WHERE l.channel='coupang' AND l.status='listed'").fetchall()
from collections import Counter
cnt = Counter()
for r in rows:
    b = match(r["title_en"], r["title_ko"])
    if b:
        cnt[b] += 1
total = sum(cnt.values())
print(f"=== 이미 listed 인데 신규 브랜드필터에 걸리는 건: {total:,} / listed {len(rows):,} ===")
for b, n in cnt.most_common():
    print(f"  {b:16s} {n:5,}")
