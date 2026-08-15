"""쿠팡 가격 정합성 감사 (read-only). 핵심: cost_usd=0/NULL(원가 미반영) + 손실가 + 마진밴드.
실제 채널가는 listings_pa.sale_krw 기준. products.sale_price_krw NULL은 설계상 정상이라 무시."""
import sqlite3
DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); c.row_factory = sqlite3.Row
L = "FROM listings_pa l JOIN products p ON p.id=l.product_id WHERE l.channel='coupang' AND l.status='listed'"
def n(sql): return c.execute(f"SELECT COUNT(*) {sql}").fetchone()[0]
base = n(L)
print(f"=== 쿠팡 listed {base:,} 가격 정합성 ===\n")

print("--- ① 아마존 원가(cost_usd) 0/NULL  ★핵심 ---")
cost0 = n(f"{L} AND (p.cost_usd IS NULL OR p.cost_usd<=0)")
print(f"  cost_usd NULL/0 인 listed: {cost0:,}  ({100*cost0/max(base,1):.2f}%)")
if cost0:
    print("  ⚠️ 샘플 (원가0인데 listed):")
    for r in c.execute(f"SELECT p.id, p.cost_usd, p.landed_price_usd lp, p.amazon_price_usd ap, l.sale_krw, ROUND(l.net_margin_pct,0) m, substr(COALESCE(p.title_ko,p.title_en),1,30) t {L} AND (p.cost_usd IS NULL OR p.cost_usd<=0) LIMIT 10"):
        print(f"    id={r['id']} cost_usd={r['cost_usd']} landed={r['lp']} amzn={r['ap']} sale={r['sale_krw']}원 마진{r['m']}% | {r['t']}")
for col in ("landed_price_usd","amazon_price_usd"):
    z = n(f"{L} AND (p.{col} IS NULL OR p.{col}<=0)")
    print(f"  ({col} NULL/0: {z:,})")

print("\n--- ② 채널가(sale_krw) 결측/0 ---")
print(f"  sale_krw NULL/0 인 listed: {n(f'{L} AND (l.sale_krw IS NULL OR l.sale_krw<=0)'):,}")
print(f"  cost_krw_snapshot NULL/0: {n(f'{L} AND (l.cost_krw_snapshot IS NULL OR l.cost_krw_snapshot<=0)'):,}")

print("\n--- ③ 손실가 (sale_krw <= cost_krw_snapshot) ★핵심 ---")
loss = n(f"{L} AND l.sale_krw>0 AND l.cost_krw_snapshot>0 AND l.sale_krw <= l.cost_krw_snapshot")
print(f"  손실/원가이하 listed: {loss:,}")
if loss:
    for r in c.execute(f"SELECT p.id, p.cost_usd, l.cost_krw_snapshot ck, l.sale_krw, ROUND(l.net_margin_pct,1) m {L} AND l.sale_krw>0 AND l.cost_krw_snapshot>0 AND l.sale_krw<=l.cost_krw_snapshot LIMIT 8"):
        print(f"    id={r['id']} cost_usd={r['cost_usd']} cost_krw={r['ck']:,.0f} sale={r['sale_krw']:,.0f} 마진{r['m']}%")

print("\n--- ④ 순마진(net_margin_pct) 분포 ---")
for lbl, cond in [("음수(손실)","l.net_margin_pct<0"),("0~10%","l.net_margin_pct>=0 AND l.net_margin_pct<10"),
                  ("10~25%","l.net_margin_pct>=10 AND l.net_margin_pct<25"),("25~40%","l.net_margin_pct>=25 AND l.net_margin_pct<40"),
                  ("40%+","l.net_margin_pct>=40"),("NULL","l.net_margin_pct IS NULL")]:
    print(f"  {lbl:10s} {n(f'{L} AND {cond}'):6,}")
print(f"  margin_risk 플래그: {n(f'{L} AND l.margin_risk=1'):,}")

print("\n--- ⑤ 마크업 검증 (sale_krw vs cost_krw, 정상 표본) ---")
r = c.execute(f"SELECT AVG(l.sale_krw) sp, AVG(l.cost_krw_snapshot) cp, AVG(l.net_margin_pct) m {L} AND l.sale_krw>0 AND l.cost_krw_snapshot>0").fetchone()
print(f"  평균 sale={r['sp']:,.0f}원  평균 cost_krw={r['cp']:,.0f}원  배율={r['sp']/max(r['cp'],1):.2f}x  평균마진={r['m']:.1f}%")
print("  정상 샘플 5건:")
for r in c.execute(f"SELECT p.id, p.cost_usd, l.cost_krw_snapshot ck, l.sale_krw, ROUND(l.net_margin_pct,1) m {L} AND l.sale_krw>0 AND l.cost_krw_snapshot>0 AND l.net_margin_pct BETWEEN 20 AND 45 LIMIT 5"):
    print(f"    id={r['id']} cost_usd=${r['cost_usd']} cost_krw={r['ck']:,.0f} → sale={r['sale_krw']:,.0f} 마진{r['m']}%")
c.close()
