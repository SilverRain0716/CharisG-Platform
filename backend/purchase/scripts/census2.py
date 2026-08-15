import os, sqlite3
from collections import Counter
from dotenv import load_dotenv
_ROOT=os.environ.get("CHARISG_ROOT","/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT,".env"))
DB=os.path.join(_ROOT,"backend/purchase/purchase.db")
def hangul(s): return sum(1 for ch in (s or "") if "가"<=ch<="힣")
from backend.purchase.services import coupang_service as cs
allp=cs.list_all_seller_products()
active=[p for p in allp if p.get("statusName")=="승인완료"]
eng=[p for p in active if hangul(p.get("sellerProductName"))<2]
con=sqlite3.connect("file:%s?mode=ro"%DB, uri=True); con.row_factory=sqlite3.Row
# displayCategoryCode -> path (도서 판정)
catmap={str(r["code"]):(r["path"] or "") for r in con.execute("SELECT code,path FROM coupang_categories")}
def is_book(p):
    return "도서" in catmap.get(str(p.get("displayCategoryCode")),"")
books=[p for p in eng if is_book(p)]
nonbooks=[p for p in eng if not is_book(p)]
# 비책 -> 우리 product 매핑 (생성일자)
spid_d={str(r["cpid"]):r["d"] for r in con.execute("SELECT l.channel_product_id cpid, substr(p.created_at,1,10) d FROM listings_pa l JOIN products p ON p.id=l.product_id WHERE l.channel='coupang' AND l.channel_product_id IS NOT NULL")}
print("승인완료 %d / 영문명 %d" % (len(active), len(eng)))
print("  도서(영문 정상): %d" % len(books))
print("  ★비도서 영문명(진짜 미번역): %d" % len(nonbooks))
print()
print("비도서 영문명 생성일자별:")
for d,n in Counter(spid_d.get(str(p.get("sellerProductId")),"?") for p in nonbooks).most_common(12):
    print("  %s: %d" % (d,n))
print()
print("비도서 영문명 샘플 (쿠팡노출명 | displayCat path):")
for p in nonbooks[:30]:
    nm=(p.get("sellerProductName") or "")[:45]; cp=catmap.get(str(p.get("displayCategoryCode")),"")[:30]
    print("  %s | %s" % (nm, cp))
