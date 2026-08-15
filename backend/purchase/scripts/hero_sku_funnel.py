"""히어로 SKU 후보 추출 (read-only). 광고/할인/검색태그 집중 대상 선별용 퍼널.
기준(1차안): listed + 한국직배 + 적정가 + 마진여력 + 수요(BSR 프록시). 단계별 잔존수로 퍼널 붕괴 지점 확인."""
import sqlite3
DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); c.row_factory = sqlite3.Row
def n(sql, p=()): return c.execute(sql, p).fetchone()[0]

L = "FROM listings_pa l JOIN products p ON p.id=l.product_id WHERE l.channel='coupang' AND l.status='listed'"
base = n(f"SELECT COUNT(*) {L}")
print(f"=== 히어로 SKU 퍼널 (base listed {base:,}) ===\n")

steps = [
  ("① 한국직배 가능", "l.kr_shipping_eligible=1"),
  ("② +적정가 1~10만원", "l.kr_shipping_eligible=1 AND l.sale_krw BETWEEN 10000 AND 100000"),
  ("③ +마진 25%+", "l.kr_shipping_eligible=1 AND l.sale_krw BETWEEN 10000 AND 100000 AND l.net_margin_pct>=25"),
  ("④ +BSR 있음(수요)", "l.kr_shipping_eligible=1 AND l.sale_krw BETWEEN 10000 AND 100000 AND l.net_margin_pct>=25 AND p.bsr IS NOT NULL AND p.bsr>0"),
]
for lbl, cond in steps:
    print(f"  {lbl:22s} {n(f'SELECT COUNT(*) {L} AND ({cond})'):6,}")

print("\n--- BSR 커버리지 (수요 프록시 가용성) ---")
print(f"  listed 중 bsr 있음: {n(f'SELECT COUNT(*) {L} AND p.bsr IS NOT NULL AND p.bsr>0'):,} / {base:,}")

HERO = f"{L} AND l.kr_shipping_eligible=1 AND l.sale_krw BETWEEN 10000 AND 100000 AND l.net_margin_pct>=25"
hero_n = n(f"SELECT COUNT(*) {HERO}")
print(f"\n=== 히어로 후보(①②③, BSR 무관) = {hero_n:,}건 ===")
print("--- 가격대 분포 ---")
for lbl, cond in [("1~3만원","l.sale_krw<30000"),("3~5만원","l.sale_krw>=30000 AND l.sale_krw<50000"),("5~10만원","l.sale_krw>=50000")]:
    print(f"  {lbl:10s} {n(f'SELECT COUNT(*) {HERO} AND {cond}'):6,}")

print("\n--- 카테고리 상위 10 (히어로 후보) ---")
for r in c.execute(f"SELECT COALESCE(CAST(l.coupang_category_code AS TEXT),'(미정)') cc, COUNT(*) cnt {HERO} GROUP BY cc ORDER BY cnt DESC LIMIT 10"):
    print(f"  {str(r['cc']):14s} {r['cnt']:5,}")

print("\n※ BSR 미수집 → 수요 프록시 부재. 마진 높은순으로 우선 제시(광고/할인 여력 큰 순).")
print("--- 추천 TOP 20 (마진 높은순) ---")
rows = c.execute(f"SELECT p.id, substr(COALESCE(p.seo_title,p.title_ko,p.title_en),1,44) t, l.sale_krw, ROUND(l.net_margin_pct,0) m "
                 f"{HERO} ORDER BY l.net_margin_pct DESC LIMIT 20").fetchall()
for r in rows:
    print(f"  id={r['id']:>6} {int(r['sale_krw']):>7,}원 마진{int(r['m']):>3}% | {r['t']}")
c.close()
