"""재분류 PUT 실패 진단 — 한 상품의 GET 현재값 / 분류 / 새 메타 / 빌드 notices·attrs 를 덤프."""
import json
import os
import sqlite3
import sys

from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")


def main(cpid, name):
    from backend.purchase.services.category_mapper import find_coupang_category_with_gemini as fc
    from backend.purchase.services.coupang_meta import get_category_meta, build_default_notices, extract_notice_category_names
    from backend.purchase.services.coupang_attributes import build_required_attributes
    from backend.purchase.services import coupang_service as cs

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    pr = con.execute("SELECT * FROM products p JOIN listings_pa l ON l.product_id=p.id "
                     "WHERE l.channel='coupang' AND l.channel_product_id=? LIMIT 1", (cpid,)).fetchone()
    pdict = dict(pr) if pr else {}
    te = pdict.get("title_en") or ""
    print("=== 상품 ===", name[:40], "| title_en:", te[:40])

    info = cs.get_seller_product(str(cpid))
    data = info.get("data") if info else None
    if isinstance(data, dict):
        print("=== GET 현재 displayCategoryCode:", data.get("displayCategoryCode"))
        items = data.get("items") or []
        if items:
            cur_notices = items[0].get("notices") or []
            print("=== GET 현재 item[0].notices (첫 3):")
            for n in cur_notices[:3]:
                print("   ", json.dumps(n, ensure_ascii=False))

    res = fc(name, sample_en=te)
    new_code = str(res.get("code") or "")
    print("=== 분류 결과: code=%s path=%s score=%s review=%s" % (new_code, res.get("path"), res.get("score"), res.get("needs_review")))

    meta = get_category_meta(new_code)
    if not meta:
        print("=== 새 메타 조회 실패!"); return
    print("=== 새 카테고리 noticeCategoryNames:", extract_notice_category_names(meta))
    notices = build_default_notices(meta)
    print("=== build_default_notices 결과 (첫 3):")
    for n in notices[:3]:
        print("   ", json.dumps(n, ensure_ascii=False))
    attrs, skip = build_required_attributes(meta, pdict, cat_path=res.get("path") or "")
    print("=== build_required_attributes: skip=%r attrs=%d" % (skip, len(attrs)))
    for a in attrs[:5]:
        print("   ", json.dumps(a, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
