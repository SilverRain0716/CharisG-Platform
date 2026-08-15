"""활성 리스팅의 정책 위반 잔재 일괄 정리 (감사 발견분, 2026-06-02).

위반 검출 (게이트 함수 = 권위):
  - 의류/신발 (영구차단): check_blocked_apparel_shoes
  - DTC 유전자검사 키트 (영구차단): check_prohibited_genetic_kit
  - 마진 < 15,000원 (손실 차단): listings_pa.net_margin_krw

액션 매트릭스:
  | 위반          | coupang                  | smartstore                    |
  | 영구(의류/DTC) | stop_sales + excluded    | delete_product + excluded     |
  | 마진<15K       | stop_sales + paused      | naver stop_sales + paused     |
  우선순위: 영구차단 > 마진 (둘 다면 excluded 처리).

CSV 백업 + done-tracking. 가역(마진=paused, 영구=excluded/네이버삭제).

  python -m backend.purchase.scripts.cleanup_policy_violations --dry-run
  python -m backend.purchase.scripts.cleanup_policy_violations --apply
"""
import argparse, os, sqlite3, time, csv, collections
from dotenv import load_dotenv
load_dotenv()
from backend.purchase.services import clean_policy as cp
from backend.purchase.services import coupang_service as cous
from backend.purchase.services import naver_commerce_service as nav

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "purchase.db")
DONE = "/home/ubuntu/logs/policy_cleanup_done.txt"
CSV_BAK = "/home/ubuntu/backups/policy_cleanup_20260602.csv"
MARGIN_FLOOR = 3000


def classify(r):
    """위반 종류 결정. 영구차단 우선. 반환 (violation, kind) 또는 (None, None)."""
    ap, ak = cp.check_blocked_apparel_shoes(r["tk"] or "")
    if ap:
        return f"의류/신발({ak})", "perm"
    dt, dk = cp.check_prohibited_genetic_kit(r["te"] or "", r["tk"] or "")
    if dt:
        return f"DTC유전자키트({dk})", "perm"
    nm = r["nm"]
    if nm is not None and nm < MARGIN_FLOOR:
        return f"마진<15K({nm})", "margin"
    return None, None


def select_targets(con):
    rows = con.execute(
        "SELECT l.id lid, l.channel ch, l.channel_product_id cpid, l.product_id pid, "
        " l.net_margin_krw nm, p.title_en te, p.title_ko tk, p.brand br, p.asin "
        "FROM listings_pa l JOIN products p ON p.id=l.product_id "
        "WHERE l.status='listed' AND l.channel_product_id IS NOT NULL AND l.channel_product_id<>''"
    ).fetchall()
    out = []
    for r in rows:
        viol, kind = classify(r)
        if viol:
            out.append((r, viol, kind))
    return out


def do_action(con, r, kind):
    """채널×kind 액션 실행. 반환 (ok, note)."""
    ch, cpid, lid = r["ch"], str(r["cpid"]), r["lid"]
    new_status = "excluded" if kind == "perm" else "paused"
    if ch == "coupang":
        ok, msg = cous.stop_sales(cpid)
        gone = False
    else:  # smartstore
        if kind == "perm":
            ok, msg = nav.delete_product(cpid)
            gone = (not ok) and ("404" in msg)
            ok = ok or gone
        else:  # margin → 판매중지(SUSPENSION), 삭제 아님
            ok, msg = nav.stop_sales(cpid)
            gone = False
    if ok:
        con.execute("UPDATE listings_pa SET status=?, error_message=? WHERE id=?",
                    (new_status, f"정책정리: {kind} 20260602" + ("[404]" if gone else ""), lid))
        con.commit()
    return ok, msg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", choices=["perm", "margin"], help="한 종류만")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.5)
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=180000")
    targets = select_targets(con)
    if args.only:
        targets = [(r, v, k) for r, v, k in targets if k == args.only]
    # 분포
    dist = collections.Counter()
    for r, v, k in targets:
        cat = v.split("(")[0]
        dist[f"{r['ch']}/{cat}"] += 1
    print(f"정책 위반 정리 대상: {len(targets)}")
    for key, c in sorted(dist.items()):
        print(f"  {key}: {c}")

    # CSV 백업
    os.makedirs(os.path.dirname(CSV_BAK), exist_ok=True)
    with open(CSV_BAK, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["listing_id", "channel", "channel_product_id", "product_id", "asin",
                    "net_margin_krw", "violation", "kind", "brand", "title_ko"])
        for r, v, k in targets:
            w.writerow([r["lid"], r["ch"], r["cpid"], r["pid"], r["asin"],
                        r["nm"], v, k, r["br"], r["tk"]])
    print(f"백업 CSV: {CSV_BAK} ({len(targets)})")

    if not apply:
        print("\n=== DRY-RUN 샘플 12 ===")
        for r, v, k in targets[:12]:
            print(f"  {r['ch']:10} {r['cpid']} | {v[:30]} | {(r['tk'] or '')[:24]}")
        print("실행: --apply [--only perm|margin]")
        return

    done = set()
    if os.path.exists(DONE):
        with open(DONE) as f:
            done = set(x.strip() for x in f if x.strip())
    todo = [(r, v, k) for r, v, k in targets if f"{r['ch']}:{r['cpid']}" not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"\n이번 처리: {len(todo)} (이미done {len(done)})")
    df = open(DONE, "a")
    ok = fail = 0
    for i, (r, v, k) in enumerate(todo):
        try:
            success, msg = do_action(con, r, k)
        except Exception as e:
            success, msg = False, f"예외:{e}"
        if success:
            ok += 1
            df.write(f"{r['ch']}:{r['cpid']}\n"); df.flush()
        else:
            fail += 1
            if fail <= 10:
                print(f"  실패 {r['ch']} {r['cpid']}: {msg[:70]}")
        if (i + 1) % 50 == 0:
            print(f"  ...{i+1}/{len(todo)} ok={ok} fail={fail}", flush=True)
        time.sleep(args.sleep)
    df.close()
    print(f"\n=== 결과 === 처리 {ok} / 실패 {fail} / 총 {len(todo)}")


if __name__ == "__main__":
    main()
