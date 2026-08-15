"""쿠팡 판매 성과 + 리스팅 품질 갭 분석 (read-only). 판매개선 레버 찾기용."""
import os, sqlite3
DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); c.row_factory = sqlite3.Row
def q1(sql, p=()): return c.execute(sql, p).fetchone()[0]

L = "FROM listings_pa l JOIN products p ON p.id=l.product_id WHERE l.channel='coupang' AND l.status='listed'"
listed = q1(f"SELECT COUNT(*) {L}")
print(f"=== 쿠팡 listed 총 {listed:,}건 ===\n")

print("--- 판매 성과 (order_count_30d) ---")
for lbl, cond in [("0건(판매없음)","=0 OR l.order_count_30d IS NULL"),("1-2건",">=1 AND l.order_count_30d<=2"),("3-9건",">=3 AND l.order_count_30d<=9"),("10건+",">=10")]:
    n = q1(f"SELECT COUNT(*) {L} AND (l.order_count_30d {cond})")
    print(f"  {lbl:14s} {n:6,}  ({100*n/max(listed,1):.1f}%)")

print("\n--- 위너 상태 (winner_status) ---")
for r in c.execute(f"SELECT COALESCE(l.winner_status,'(미확인)') ws, COUNT(*) n {L} GROUP BY ws ORDER BY n DESC"):
    print(f"  {r['ws']:18s} {r['n']:6,}  ({100*r['n']/max(listed,1):.1f}%)")

print("\n--- 리스팅 경과일 (days_listed) ---")
for lbl, cond in [("≤7일","<=7"),("8-30일",">7 AND l.days_listed<=30"),("31-90일",">30 AND l.days_listed<=90"),("90일+",">90")]:
    n = q1(f"SELECT COUNT(*) {L} AND l.days_listed {cond}")
    print(f"  {lbl:10s} {n:6,}")
recent_order = q1(f"SELECT COUNT(*) {L} AND l.last_order_at >= date('now','-30 day')")
ever_order = q1(f"SELECT COUNT(*) {L} AND l.last_order_at IS NOT NULL")
print(f"  최근30일 주문발생: {recent_order:,} | 역대 주문有: {ever_order:,} ({100*ever_order/max(listed,1):.1f}%)")

print("\n--- 콘텐츠/SEO 완성도 (listed 중) ---")
for lbl, cond in [("seo_title 있음","p.seo_title IS NOT NULL AND p.seo_title<>''"),
                  ("seo_tags 있음","p.seo_tags IS NOT NULL AND p.seo_tags<>''"),
                  ("검색태그 동기화","p.coupang_search_tags_synced_at IS NOT NULL"),
                  ("이미지 3장+","json_array_length(p.images_json) >= 3"),
                  ("description_ko 있음","p.description_ko IS NOT NULL AND p.description_ko<>''")]:
    n = q1(f"SELECT COUNT(*) {L} AND ({cond})")
    print(f"  {lbl:18s} {n:6,}  ({100*n/max(listed,1):.1f}%)")

print("\n--- 가격/할인/마진 (listed 중) ---")
disc = q1(f"SELECT COUNT(*) {L} AND l.discount_krw IS NOT NULL AND l.discount_krw>0")
print(f"  할인 적용: {disc:,} ({100*disc/max(listed,1):.1f}%)")
kr = q1(f"SELECT COUNT(*) {L} AND l.kr_shipping_eligible=1")
print(f"  한국직배 가능: {kr:,} ({100*kr/max(listed,1):.1f}%)")
r = c.execute(f"SELECT AVG(l.sale_krw) avg_p, AVG(l.net_margin_pct) avg_m, MIN(l.sale_krw) mn, MAX(l.sale_krw) mx {L} AND l.sale_krw>0").fetchone()
print(f"  평균가 {r['avg_p']:,.0f}원 | 평균마진 {r['avg_m']:.1f}% | 범위 {r['mn']:,.0f}~{r['mx']:,.0f}원")

print("\n--- 판매된 상품 vs 안팔린 상품 특성 비교 ---")
for lbl, oc in [("판매有(order>=1)", ">=1"), ("판매無(order=0)", "=0 OR l.order_count_30d IS NULL")]:
    base = f"{L} AND (l.order_count_30d {oc})"
    tot = q1(f"SELECT COUNT(*) {base}")
    if not tot: continue
    avgp = q1(f"SELECT AVG(l.sale_krw) {base} AND l.sale_krw>0") or 0
    tag = q1(f"SELECT COUNT(*) {base} AND p.coupang_search_tags_synced_at IS NOT NULL")
    disc2 = q1(f"SELECT COUNT(*) {base} AND l.discount_krw>0")
    win = q1(f"SELECT COUNT(*) {base} AND l.winner_status='WINNER'")
    print(f"  [{lbl}] n={tot:,} 평균가={avgp:,.0f}원 검색태그={100*tag/tot:.0f}% 할인={100*disc2/tot:.0f}% 위너={100*win/tot:.0f}%")
c.close()
