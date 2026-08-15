"""DB엔 GTIN 있는데 쿠팡엔 바코드 미등록인 상품 파악 (샘플 추정).

사용자 지시(2026-05-25): DB identifiers_json 에 GTIN 있는데 쿠팡 listing 에 바코드 안 들어간 케이스.
방법: coupang listed + identifiers_json 보유 상품 샘플 → get_seller_product → items[].barcode 확인.
"""
import argparse
import json
import os
import sqlite3

from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
DB = os.path.join(_ROOT, "backend/purchase/purchase.db")


def extract_gtin(ij):
    """identifiers_json 에서 EAN/UPC/GTIN 값 추출 (형식 유연 대응)."""
    if not ij:
        return None
    try:
        data = json.loads(ij)
    except Exception:
        return None
    found = []
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str) and v.isdigit() and 8 <= len(v) <= 14:
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(data)
    return found[0] if found else None


def main(limit):
    from backend.purchase.services import coupang_service as cs
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT p.id, p.identifiers_json ij, l.channel_product_id cpid "
        "FROM products p JOIN listings_pa l ON l.product_id=p.id "
        "WHERE l.channel='coupang' AND l.status='listed' AND l.channel_product_id IS NOT NULL "
        "AND p.identifiers_json IS NOT NULL AND p.identifiers_json NOT IN ('','[]','null') "
        "ORDER BY RANDOM() LIMIT ?",
        (limit,),
    ).fetchall()

    # 형식 1회 출력
    if rows:
        print("identifiers_json 형식 샘플:", (rows[0]["ij"] or "")[:200])
        print()

    db_gtin = cu_has = cu_empty = no_db_gtin = qfail = 0
    samples = []
    for r in rows:
        g = extract_gtin(r["ij"])
        if not g:
            no_db_gtin += 1
            continue
        db_gtin += 1
        d = cs.get_seller_product(str(r["cpid"]))
        if not d:
            qfail += 1
            continue
        items = (d.get("data") or {}).get("items") or []
        barcodes = [it.get("barcode") for it in items]
        if any(b and str(b).strip() for b in barcodes):
            cu_has += 1
        else:
            cu_empty += 1
            if len(samples) < 15:
                ebr = [it.get("emptyBarcodeReason") for it in items][:1]
                samples.append((r["id"], r["cpid"], g, ebr))

    print(f"샘플 {len(rows)} | DB GTIN 추출됨 {db_gtin} (추출불가 {no_db_gtin}) | 조회실패 {qfail}")
    print(f"  쿠팡 바코드 있음: {cu_has}")
    print(f"  ★쿠팡 바코드 없음(DB엔 GTIN 있음): {cu_empty}  ({100*cu_empty/max(db_gtin,1):.0f}%)")
    print()
    print("바코드 미등록 샘플 (pid | cpid | DB GTIN | emptyBarcodeReason):")
    for pid, cpid, g, ebr in samples:
        print(f"   {pid} | {cpid} | {g} | {ebr}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()
    main(args.limit)
