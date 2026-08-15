"""NO_VID 29 ASIN — DB ↔ 쿠팡 sellerProduct items 매칭 진단."""
import sys, os, sqlite3, json
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.services.variation import clean_axis_value
from backend.purchase.services.coupang_service import get_seller_product

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
conn = sqlite3.connect(DB, timeout=180)
conn.row_factory = sqlite3.Row

# all_drop 옵션 추출 (v2.2)
rows = conn.execute("""
  SELECT lo.option_label, p.asin, l.channel_product_id spid
  FROM listing_options lo
  JOIN listings_pa l ON l.id=lo.listing_id
  JOIN products p ON p.id=lo.child_product_id
  WHERE l.channel='coupang' AND l.status='listed' AND lo.status='active'
    AND l.channel_product_id IS NOT NULL AND l.channel_product_id != ''
""").fetchall()

# 우리 시점 all_drop 집합
targets = {}
for r in rows:
    tokens = [t.strip() for t in (r["option_label"] or "").split("/") if t.strip()]
    if tokens and all(clean_axis_value(t) is None for t in tokens):
        targets.setdefault(r["spid"], []).append(r["asin"])

# spid 별 진단
for spid, asins in targets.items():
    info = get_seller_product(str(spid))
    if not info or not isinstance(info.get("data"), dict):
        print(f"\n[FAIL] spid={spid} 조회 실패")
        continue
    items = info["data"].get("items") or []
    sku_to_vid = {}
    sku_no_vid = set()
    for it in items:
        sku = it.get("externalVendorSku")
        vid = it.get("vendorItemId")
        if not sku:
            continue
        if vid:
            sku_to_vid[sku] = (vid, it.get("statusName"), it.get("salePrice"))
        else:
            sku_no_vid.add((sku, it.get("statusName")))
    print(f"\n[spid={spid}] items {len(items)}, DB targets {len(asins)}")
    print(f"  vendorItemId 있는 sku: {len(sku_to_vid)}, vendorItemId None sku: {len(sku_no_vid)}")
    if sku_no_vid:
        print(f"  vendorItemId None 샘플: {list(sku_no_vid)[:5]}")
    no_vid_asins = [a for a in asins if a not in sku_to_vid]
    print(f"  실 NO_VID (vid None or sku 없음): {len(no_vid_asins)} → {no_vid_asins[:5]}")
    # 우리 DB 에 그 ASIN 의 children_json/product 상태
    for asin in no_vid_asins[:3]:
        # listings_pa 또는 다른 sellerProduct 의 externalVendorSku 인지
        other = conn.execute("""
          SELECT l.channel_product_id spid2, p.asin
          FROM listings_pa l JOIN products p ON p.id=l.product_id
          WHERE p.asin=? AND l.channel='coupang' AND l.status='listed'
        """, (asin,)).fetchall()
        print(f"    {asin}: 다른 listings_pa 존재 = {[r['spid2'] for r in other]}")
        # ASIN 이 쿠팡 어디 sellerProduct 의 children 인지 — items 의 itemName 안에 들어있나
        for it in items:
            sku = it.get("externalVendorSku") or ""
            name = it.get("itemName") or ""
            if asin in name or asin == sku:
                print(f"      [match-by-name] vid={it.get('vendorItemId')} sku={sku} name='{name[:50]}'")
conn.close()
