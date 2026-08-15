"""네이버(smartstore) KC 인증 위험 상품 완전 삭제 + DB 격리.

대상(가구 포함, 638 범위):
  - 완구/캐릭터 브랜드 (check_kc_blocked brand)
  - 전기/생활 KC비면제 키워드 (check_kc_blocked)
  - 어린이 카테고리: 출산/육아>완구/인형, 어린이식기, 유아동문구
  - 아동/주니어 가구
제외(오탐): 반려동물(강아지/고양이), DVD/블루레이(미디어).

쿠팡 어린이제품 판매중지(어린이제품안전특별법) 대응 — 구매대행은 KC 면제 불가라
삭제가 정답. delete_product = DELETE /v2/products/origin-products/{originProductNo} (비가역).
404(이미 삭제됨)도 excluded 마킹. 삭제 전 CSV 백업.

  python -m backend.purchase.scripts.delete_naver_kc_risk --dry-run
  python -m backend.purchase.scripts.delete_naver_kc_risk --apply
"""
import argparse, os, sqlite3, time, csv, collections
from dotenv import load_dotenv
load_dotenv()
from backend.purchase.services import clean_policy as cp
from backend.purchase.services.naver_commerce_service import delete_product

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "purchase.db")
DONE = "/home/ubuntu/logs/naver_kc_delete_done.txt"
CSV_BAK = "/home/ubuntu/backups/naver_kc_delete_20260602.csv"


def is_child_cat(c):
    if not c:
        return False
    if "반려" in c or "강아지" in c or "고양이" in c:
        return False  # 펫 제외
    if "DVD" in c or "블루레이" in c:
        return False  # 미디어 제외
    if "출산/육아>완구/인형" in c:
        return True
    if "어린이식기" in c or "유아동문구" in c:
        return True
    if "아동/주니어가구" in c:  # 가구 포함(638 범위)
        return True
    return False


def select_targets(con):
    rows = con.execute(
        "SELECT l.id lid, l.channel_product_id cpid, l.product_id pid, l.status, "
        " p.title_en, p.title_ko, p.brand, p.asin, "
        " (SELECT nc.whole_name FROM naver_categories nc WHERE nc.id=l.category_mapped) cat "
        "FROM listings_pa l JOIN products p ON p.id=l.product_id "
        "WHERE l.channel='smartstore' AND l.status IN ('listed','rotated','paused') "
        "AND l.channel_product_id IS NOT NULL AND l.channel_product_id<>''"
    ).fetchall()
    out = []
    for r in rows:
        b, reason = cp.check_kc_blocked(r["title_en"] or "", r["title_ko"] or "",
                                        coupang_category_code=None, brand=r["brand"] or "")
        cflag = is_child_cat(r["cat"])
        if b or cflag:
            why = reason if b else "어린이 카테고리"
            out.append((r, why))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.4)
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=180000")
    targets = select_targets(con)
    print(f"네이버 KC위험 삭제 대상: {len(targets)}")
    bc = collections.Counter()
    for r, why in targets:
        key = "완구브랜드" if "완구 브랜드" in why else ("어린이카테고리" if why == "어린이 카테고리" else "전기/생활KC")
        bc[key] += 1
    for k, v in bc.most_common():
        print(f"  {k}: {v}")

    # CSV 백업 (항상)
    os.makedirs(os.path.dirname(CSV_BAK), exist_ok=True)
    with open(CSV_BAK, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["listing_id", "originProductNo", "product_id", "asin", "status", "brand", "category", "title_ko", "why"])
        for r, why in targets:
            w.writerow([r["lid"], r["cpid"], r["pid"], r["asin"], r["status"],
                        r["brand"], r["cat"], r["title_ko"], why])
    print(f"백업 CSV: {CSV_BAK} ({len(targets)})")

    if not apply:
        print("\n=== DRY-RUN 샘플 10 ===")
        for r, why in targets[:10]:
            print(f"  {r['cpid']} | {r['brand']} | {(r['cat'] or '')[:30]} | {why[:24]}")
        print("실행: --apply")
        return

    done = set()
    if os.path.exists(DONE):
        with open(DONE) as f:
            done = set(x.strip() for x in f if x.strip())
    todo = [(r, why) for r, why in targets if str(r["cpid"]) not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"\n이번 삭제 대상: {len(todo)} (이미done {len(done)})")
    df = open(DONE, "a")
    ok = gone = fail = 0
    for i, (r, why) in enumerate(todo):
        cpid = str(r["cpid"])
        try:
            success, err = delete_product(cpid)
        except Exception as e:
            success, err = False, f"예외:{e}"
        is_404 = (not success) and ("status=404" in err or "404" in err)
        if success or is_404:
            (ok if success else gone)
            if success:
                ok += 1
            else:
                gone += 1
            df.write(cpid + "\n"); df.flush()
            con.execute(
                "UPDATE listings_pa SET status='excluded', "
                "error_message=? WHERE id=?",
                (f"네이버 KC위험 삭제({why})20260602" + ("" if success else "[404已删]"), r["lid"]))
            con.commit()
        else:
            fail += 1
            if fail <= 8:
                print(f"  실패 {cpid}: {err[:80]}")
        if (i + 1) % 50 == 0:
            print(f"  ...{i+1}/{len(todo)} del={ok} 이미삭제={gone} fail={fail}", flush=True)
        time.sleep(args.sleep)
    df.close()
    print(f"\n=== 결과 === 삭제 {ok} / 이미삭제(404) {gone} / 실패 {fail} / 총 {len(todo)}")


if __name__ == "__main__":
    main()
