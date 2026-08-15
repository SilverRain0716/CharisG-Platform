"""오매핑 규모 산정 — 식품 카테고리 상품 샘플을 재분류해 '식품→비식품 고신뢰' 비율 추정."""
import argparse
import os
import sqlite3

from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")


def top(path):
    p = str(path or "")
    return p.split("]")[-1].split(">")[0].strip() if "]" in p else p.split(">")[0].strip()


def main(n):
    import openpyxl
    from backend.purchase.services.category_mapper import find_coupang_category_with_gemini as fc
    wb = openpyxl.load_workbook("/tmp/req_attrs.xlsx", data_only=True)
    ws = wb["템플릿"]
    rows = [r for r in ws.iter_rows(min_row=7, values_only=True) if r[0] and r[0] != "Inventory ID"]
    food = [r for r in rows if "식품" in str(r[6] or "")]
    total_food = len(food)
    step = max(1, total_food // n)
    sample = food[::step][:n]
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    misclass = []     # 식품→비식품 score>=90 review=False (진짜 오매핑)
    nonfood_low = 0   # 식품→비식품 but 저신뢰/review
    food_stays = 0    # 식품→식품
    err = 0
    for r in sample:
        name = str(r[1] or "")
        pr = con.execute(
            "SELECT title_en FROM products p JOIN listings_pa l ON l.product_id=p.id "
            "WHERE l.channel='coupang' AND l.channel_product_id=? LIMIT 1", (str(r[0]),),
        ).fetchone()
        te = pr["title_en"] if pr else ""
        try:
            res = fc(name, sample_en=te)
        except Exception:
            err += 1
            continue
        st = top(res.get("path"))
        if not st:
            err += 1
            continue
        if st != "식품":
            if res.get("score", 0) >= 90 and not res.get("needs_review"):
                misclass.append((name[:30], st, res.get("score")))
            else:
                nonfood_low += 1
        else:
            food_stays += 1

    valid = len(sample) - err
    rate = len(misclass) / valid if valid else 0
    print(f"식품 카테고리 총: {total_food}")
    print(f"샘플: {len(sample)} (유효 {valid}, 분류오류 {err})")
    print(f"  식품→비식품 고신뢰(진짜 오매핑): {len(misclass)} ({100*rate:.1f}%)")
    print(f"  식품→비식품 저신뢰/review: {nonfood_low}")
    print(f"  식품 유지: {food_stays}")
    print(f"  ⇒ 전체 추정 진짜 오매핑: ~{int(rate*total_food)}건")
    print()
    print("진짜 오매핑 샘플 (상품명 | →대분류 | score):")
    for nm, st, sc in misclass[:25]:
        print("  %s | %s | %s" % (nm, st, sc))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=150)
    main(ap.parse_args().n)
