"""시트 임포트 dry-run 검증 — 실제 임포트 전에 임포터 방식 그대로 읽어 호환성 확인.
헤더/탭/파싱결과만 출력, DB 쓰기 없음."""
import os
import sys
from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)


def main(url):
    from backend.purchase.services import sheet_importer as si
    sid = si.extract_sheet_id(url)
    print("sheet_id =", sid)
    try:
        _r = si.discover_tabs(sid)
        tabs = _r[0] if isinstance(_r, tuple) else _r
    except Exception as e:
        print("★discover_tabs 실패:", e); return
    print("탭:", tabs)
    for t in tabs:
        rows = si.fetch_tab_csv(sid, t["gid"])
        print(f"\n=== 탭 '{t['name']}' (gid={t['gid']}) — {len(rows)}행 ===")
        if not rows:
            print("  ★CSV 0행 (업로드 xlsx면 export 실패 가능 — 네이티브 변환 필요)")
            continue
        print("  헤더:", list(rows[0].keys()))
        ok = 0; asin_from_url = 0; has_price = 0
        samples = []
        for r in rows:
            pr = si.parse_row(r)
            if pr:
                ok += 1
                if pr.get("price_usd") or pr.get("price_krw"):
                    has_price += 1
                if len(samples) < 3:
                    samples.append(pr)
        print(f"  파싱 성공(유효 ASIN): {ok}/{len(rows)} | 가격 있는 행: {has_price}")
        for s in samples:
            print(f"   · asin={s['asin']} title={str(s.get('title'))[:30]} usd={s.get('price_usd')} krw={s.get('price_krw')}")
        # 중복 체크: 시트 ASIN 중 이미 products/coupang-listed 인 비율
        import sqlite3
        asins = [si.parse_row(r)["asin"] for r in rows if si.parse_row(r)]
        con = sqlite3.connect(f"file:{os.path.join(_ROOT,'backend/purchase/purchase.db')}?mode=ro", uri=True)
        prod = {a for (a,) in con.execute("SELECT DISTINCT asin FROM products WHERE asin IS NOT NULL")}
        listed = {a for (a,) in con.execute("SELECT DISTINCT pr.asin FROM listings_pa l JOIN products pr ON pr.id=l.product_id WHERE l.channel='coupang' AND l.status IN ('listed','pending')")}
        in_prod = sum(1 for a in asins if a in prod)
        in_listed = sum(1 for a in asins if a in listed)
        uniq = sum(1 for a in asins if a not in prod)
        print(f"  중복: products에 이미 있음 {in_prod} | 쿠팡 listed/pending {in_listed} | 완전신규(products에 없음) {uniq}")


if __name__ == "__main__":
    main(sys.argv[1])
