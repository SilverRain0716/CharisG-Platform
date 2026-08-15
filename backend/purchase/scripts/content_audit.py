"""콘텐츠/썸네일 품질 감사 (read-only). seo_title 플레이스홀더 오염 + 이미지 구조 점검."""
import sqlite3
DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); c.row_factory = sqlite3.Row
L = "FROM listings_pa l JOIN products p ON p.id=l.product_id WHERE l.channel='coupang' AND l.status='listed'"
def n(sql, p=()): return c.execute(sql, p).fetchone()[0]
base = n(f"SELECT COUNT(*) {L}")
print(f"=== 콘텐츠 감사 (listed {base:,}) ===\n")

print("--- seo_title 플레이스홀더 오염 ---")
pats = ["[브랜드", "브랜드명]", "[제조사", "[색상", "[사이즈", "[brand", "[모델"]
import functools
cond = " OR ".join([f"p.seo_title LIKE '%{x}%'" for x in pats])
poll = n(f"SELECT COUNT(*) {L} AND ({cond})")
print(f"  플레이스홀더 포함 seo_title: {poll:,} ({100*poll/max(base,1):.1f}%)")
for r in c.execute(f"SELECT p.id, p.seo_title {L} AND ({cond}) LIMIT 6"):
    print(f"    id={r['id']}: {r['seo_title'][:60]}")

print("\n--- seo_title 길이 분포 ---")
for lbl, cnd in [("≤10자(과단)","length(p.seo_title)<=10"),("11~50자","length(p.seo_title) BETWEEN 11 AND 50"),("51~100자","length(p.seo_title) BETWEEN 51 AND 100"),("100자+","length(p.seo_title)>100")]:
    print(f"  {lbl:12s} {n(f'SELECT COUNT(*) {L} AND {cnd}'):6,}")

print("\n--- 이미지 구조 (썸네일=images_json[0]) ---")
for lbl, cnd in [("이미지 0장(썸네일 없음)","p.images_json IS NULL OR json_array_length(p.images_json)=0"),
                 ("1~2장","json_array_length(p.images_json) BETWEEN 1 AND 2"),
                 ("3~6장","json_array_length(p.images_json) BETWEEN 3 AND 6"),
                 ("7장+","json_array_length(p.images_json)>=7")]:
    print(f"  {lbl:20s} {n(f'SELECT COUNT(*) {L} AND ({cnd})'):6,}")

print("\n--- 샘플 썸네일 URL (hero 후보 3건, 육안확인용) ---")
HERO = f"{L} AND l.kr_shipping_eligible=1 AND l.sale_krw BETWEEN 10000 AND 100000 AND l.net_margin_pct>=25"
import json
for r in c.execute(f"SELECT p.id, p.images_json {HERO} AND p.images_json IS NOT NULL ORDER BY l.net_margin_pct DESC LIMIT 3"):
    try:
        imgs = json.loads(r['images_json'])
        url = imgs[0] if isinstance(imgs, list) and imgs else None
        if isinstance(url, dict): url = url.get('url') or url.get('link')
        print(f"  id={r['id']}: {url}")
    except Exception as e:
        print(f"  id={r['id']}: parse err {e}")
c.close()
