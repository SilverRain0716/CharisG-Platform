"""재분류 정확도 테스트 — 필수속성 파일의 오매핑 상품 20건을 분류기 기본값으로 재분류해 비교.
읽기전용 (실제 변경 없음)."""
import os
import sqlite3

from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")


def main():
    import openpyxl
    from backend.purchase.services.category_mapper import find_coupang_category_with_gemini as fc
    wb = openpyxl.load_workbook("/tmp/req_attrs.xlsx", data_only=True)
    ws = wb["템플릿"]
    rows = [r for r in ws.iter_rows(min_row=7, values_only=True) if r[0] and r[0] != "Inventory ID"]
    step = max(1, len(rows) // 20)
    picks = rows[::step][:20]

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    for r in picks:
        cpid = str(r[0]); name = str(r[1] or ""); cur = str(r[6] or "")
        pr = con.execute(
            "SELECT title_en FROM products p JOIN listings_pa l ON l.product_id=p.id "
            "WHERE l.channel='coupang' AND l.channel_product_id=? LIMIT 1", (cpid,),
        ).fetchone()
        te = pr["title_en"] if pr else ""
        try:
            res = fc(name, sample_en=te)
        except Exception as e:
            print("ERR", cpid, e); continue
        cur_short = cur.split("]")[-1].strip()[:22]
        print("● %s" % name[:34])
        print("   현재: %s" % cur_short)
        print("   →제안: %s (score %s, review=%s)" % ((res.get("path") or "")[:50], res.get("score"), res.get("needs_review")))


if __name__ == "__main__":
    main()
