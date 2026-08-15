"""카테고리 변경 PUT 생애주기 추적 — 승인완료 상품의 category 변경이 어느 시점에 되돌아가는지 확인.
GET(전) → PUT(새 category+attrs+notices) → GET(직후) → request_approval → GET(5s) → GET(40s).
읽기+1상품 PUT (검증 목적). 비도서('기타 재화' 사용 가능) 상품 대상."""
import os
import sqlite3
import sys
import time

from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")

from backend.purchase.scripts.recategorize_food import pick_notices


def cat_of(cs, cpid):
    info = cs.get_seller_product(str(cpid))
    d = info.get("data") if info else {}
    return (d.get("displayCategoryCode"), d.get("statusName")) if isinstance(d, dict) else (None, None)


def predict_code(cs, name, brand=""):
    import requests
    path = "/v2/providers/openapi/apis/api/v1/categorization/predict"
    r = requests.post(cs.BASE + path, headers=cs._signature("POST", path),
                      json={"productName": name, "brand": brand or "", "productDescription": ""}, timeout=20)
    d = (r.json() or {}).get("data") or {}
    return str(d.get("predictedCategoryId") or ""), d.get("predictedCategoryName") or ""


def main(cpid, name):
    from backend.purchase.services.coupang_meta import get_category_meta
    from backend.purchase.services.coupang_attributes import build_required_attributes
    from backend.purchase.services import coupang_service as cs

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    pr = con.execute("SELECT * FROM products p JOIN listings_pa l ON l.product_id=p.id "
                     "WHERE l.channel='coupang' AND l.channel_product_id=? LIMIT 1", (cpid,)).fetchone()
    pdict = dict(pr) if pr else {}
    if not name:
        name = pdict.get("title_ko") or pdict.get("title_en") or ""

    print("[0] GET 전:", cat_of(cs, cpid))
    new_code, pname = predict_code(cs, name, pdict.get("brand") or "")
    print("[쿠팡추천] code=%s name=%s" % (new_code, pname))
    meta = get_category_meta(new_code)
    cat_path = pname  # predict 는 path 없이 leaf name 만 → cat_path 휴리스틱용
    attrs, skip = build_required_attributes(meta, pdict, cat_path=cat_path)
    notices = pick_notices(meta, cat_path)
    print("[빌드] attrs=%d notices=%s skip=%r" % (len(attrs), (len(notices) if notices is not None else None), skip))
    if skip or notices is None:
        print("→ 빌드 실패, 중단"); return

    info = cs.get_seller_product(str(cpid))
    data = info["data"]
    data["displayCategoryCode"] = int(new_code)
    for it in data.get("items") or []:
        it["attributes"] = attrs
        it["notices"] = notices
    path = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
    r = cs._request_with_retry("PUT", cs.BASE + path, headers=cs._signature("PUT", path), json=data, timeout=30)
    body = r.json() if r and r.text else {}
    ok = r is not None and r.status_code < 400 and body.get("code") != "ERROR"
    print("[PUT] ok=%s code=%s msg=%s" % (ok, body.get("code"), "; ".join(cs._extract_error_messages(body))[:120]))
    time.sleep(3)
    print("[1] PUT 직후 GET:", cat_of(cs, cpid), "(목표:", new_code, ")")
    a_ok, aerr = cs.request_approval(str(cpid))
    print("[승인요청] ok=%s err=%s" % (a_ok, str(aerr)[:80]))
    time.sleep(5)
    print("[2] 승인 5s 후:", cat_of(cs, cpid))
    time.sleep(40)
    print("[3] 승인 45s 후:", cat_of(cs, cpid), "(목표:", new_code, ")")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
