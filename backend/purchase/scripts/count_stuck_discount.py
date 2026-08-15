"""할인 적용됐는데 Amazon 할인은 끝난(복귀 안 된) 상품 집계 (read-only)."""
import sqlite3
DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); c.row_factory = sqlite3.Row
B = ("FROM listings_pa l JOIN products p ON p.id=l.product_id "
     "WHERE l.channel='coupang' AND l.status='listed' AND l.discount_krw IS NOT NULL AND l.discount_krw>0")
STILL = (" AND p.amazon_price_usd>0 AND p.landed_price_usd>0 AND p.landed_price_usd<p.amazon_price_usd "
         "AND (p.amazon_price_usd-p.landed_price_usd)/p.amazon_price_usd>=0.20")
disc = c.execute("SELECT COUNT(*) " + B).fetchone()[0]
still = c.execute("SELECT COUNT(*) " + B + STILL).fetchone()[0]
print(f"할인가 적용된 listed: {disc:,}")
print(f"  └ 현재도 Amazon 20%+ 할인 유지(fill_discount 관리중): {still:,}")
print(f"  └ ★Amazon 할인 끝/축소인데 할인가에 묶임(복귀 안 됨): {disc-still:,}")
print("  샘플(할인 끝났는데 할인가 유지):")
for r in c.execute("SELECT p.asin a, l.sale_krw s, l.discount_krw d, p.amazon_price_usd ap, p.landed_price_usd lp "
                   + B + " AND NOT(p.landed_price_usd<p.amazon_price_usd AND "
                   "(p.amazon_price_usd-p.landed_price_usd)/p.amazon_price_usd>=0.20) LIMIT 8"):
    s, d = int(r["s"] or 0), int(r["d"] or 0)
    gap = f"할인가가 정가보다 {round(100*(s-d)/s)}%↓" if s else ""
    print(f"    {r['a']} 정가{s:,}→할인가{d:,} ({gap}) | amazon=${r['ap']} landed=${r['lp']}")
c.close()
