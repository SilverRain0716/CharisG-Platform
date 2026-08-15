"""어린이제품(KC) listed → stop_sales 판매중지 + DB 격리. 계정정지 위험 방지.

대상: 완구/캐릭터 브랜드 + 인형/doll/figure (바비큐·오븐 등 오탐 제외).
가역적(재개 가능). done-tracking + 소켓타임아웃.

  python -m backend.purchase.scripts.stop_children_products --dry-run
  python -m backend.purchase.scripts.stop_children_products --apply
"""
import argparse, os, sqlite3, time, socket, collections
socket.setdefaulttimeout(20)
from dotenv import load_dotenv
load_dotenv()
from backend.purchase.services import coupang_service as cs

DB_PATH = os.environ.get("PA_DB_PATH",
                         str(os.path.join(os.path.dirname(__file__), "..", "purchase.db")))
DONE = "/home/ubuntu/logs/children_stop_done.txt"

# 어린이 완구/캐릭터 브랜드 (성인수집·펫 제외한 진짜 어린이제품).
# 키워드(인형/doll)는 펫·보관용품·Dolly·성인피규어 오탐이 커서 제외, 브랜드만 사용.
TOY_BRANDS = [
    "Barbie", "Mattel", "Hot Wheels", "LEGO", "Fisher-Price", "Hasbro", "Play-Doh",
    "Nerf", "Melissa & Doug", "Calico Critters", "Playmobil", "Paw Patrol", "Bluey",
    "Little Tikes", "VTech", "Crayola", "Step2", "Klutz", "Ravensburger", "Aurora",
    "Spin Master", "MGA", "Schleich", "CoComelon", "Pokemon", "Breyer",
    "KIDS PREFERRED", "Squishmallows",
]


def select_targets(con):
    tb = ",".join("?" * len(TOY_BRANDS))
    sql = (
        f"SELECT l.id lid, l.channel_product_id cpid, l.product_id pid, p.brand, "
        f"substr(p.title_ko,1,36) tko "
        f"FROM listings_pa l JOIN products p ON p.id=l.product_id "
        f"WHERE l.channel='coupang' AND l.status IN ('listed','paused') "
        f"AND l.channel_product_id IS NOT NULL AND l.channel_product_id != '' "
        f"AND p.brand IN ({tb})"
    )
    return con.execute(sql, TOY_BRANDS).fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.7)
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=180000")
    targets = select_targets(con)
    print(f"어린이제품 대상(listed): {len(targets)}")
    # 브랜드 분포
    bc = collections.Counter(r["brand"] or "(무)" for r in targets)
    for b, c in bc.most_common(15):
        print(f"  {b}: {c}")

    if not apply:
        print("\n=== DRY-RUN 샘플 10 ===")
        for r in targets[:10]:
            print(f"  {r['cpid']} | {r['brand']} | {r['tko']}")
        print("실행: --apply")
        return

    done = set()
    if os.path.exists(DONE):
        with open(DONE) as f:
            done = set(x.strip() for x in f if x.strip())
    todo = [r for r in targets if str(r["cpid"]) not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"\n이번 stop 대상: {len(todo)} (이미done {len(done)})")
    df = open(DONE, "a")
    ok = hold = fail = 0
    for i, r in enumerate(todo):
        cpid = str(r["cpid"])
        try:
            success, msg = cs.stop_sales(cpid)
        except Exception as e:
            success, msg = False, f"예외:{e}"
        if success:
            ok += 1
            df.write(cpid + "\n"); df.flush()
            con.execute(
                "UPDATE listings_pa SET status='excluded', "
                "error_message='어린이제품 KC 차단(쿠팡 판매중지 위험) 20260602' WHERE id=?",
                (r["lid"],))
            con.commit()
        elif "item 실패" in msg or "items 비어" in msg:
            hold += 1
        else:
            fail += 1
        if (i + 1) % 50 == 0:
            print(f"  ...{i+1}/{len(todo)} ok={ok} hold={hold} fail={fail}", flush=True)
        time.sleep(args.sleep)
    df.close()
    print(f"\n=== 결과 === stop {ok} / 보류(심사중) {hold} / 실패 {fail} / 총 {len(todo)}")


if __name__ == "__main__":
    main()
