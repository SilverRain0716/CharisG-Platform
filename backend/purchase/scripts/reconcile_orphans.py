"""P1 정합성 리컨실 — 쿠팡 orphan(쿠팡有/우리DB無) 흡수 + 중복리스팅(P3) 식별.

orphan_spids.txt (spid<TAB>statusName) 각 건에 대해:
  1. 쿠팡 GET → sellerProductName
  2. products 이름 정확매칭 + 단일 ASIN (오매칭 방지)
  3. 그 product(또는 ASIN 형제)에 coupang listing 있나?
     - 없음 → listings_pa 행 INSERT (흡수). status 는 statusName 매핑.
     - 있음 → 이 spid 는 중복 리스팅 → dup_listing 으로 기록(P3 대상), 등록 안 함.

  python -m backend.purchase.scripts.reconcile_orphans --orphan-file /home/ubuntu/logs/orphan_spids.txt --dry-run
  python -m backend.purchase.scripts.reconcile_orphans --orphan-file /home/ubuntu/logs/orphan_spids.txt --apply
"""
import argparse, os, sqlite3, time, collections, socket
socket.setdefaulttimeout(20)  # 소켓 레벨 hang 방지 (requests timeout 안 먹는 케이스 안전망)
from dotenv import load_dotenv
load_dotenv()
from backend.purchase.services import coupang_service as cs

DB_PATH = os.environ.get("PA_DB_PATH",
                         str(os.path.join(os.path.dirname(__file__), "..", "purchase.db")))
STATUS_MAP = {"승인완료": "listed", "승인대기중": "pending", "심사중": "pending",
              "승인반려": "rejected", "임시저장": "draft"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orphan-file", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    orphans = []
    with open(args.orphan_file) as f:
        for line in f:
            p = line.strip().split("\t")
            if p and p[0]:
                orphans.append((p[0], p[1] if len(p) > 1 else "?"))
    if args.limit:
        orphans = orphans[: args.limit]

    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=180000")

    # resume 가드 — 이미 listings_pa 에 있는 spid 는 skip (재실행/스톨복구 안전)
    known_cpids = set(str(r["channel_product_id"]) for r in con.execute(
        "SELECT channel_product_id FROM listings_pa WHERE channel='coupang' "
        "AND channel_product_id IS NOT NULL").fetchall())

    # products 이름 인덱스 (title_ko/seo_title → [(id, asin)])
    idx = collections.defaultdict(list)
    for r in con.execute("SELECT id, title_ko, seo_title, asin FROM products"):
        for key in (r["title_ko"], r["seo_title"]):
            if key:
                idx[key.strip()].append((r["id"], r["asin"]))

    def has_listing(asin):
        r = con.execute(
            "SELECT l.id, l.channel_product_id FROM listings_pa l JOIN products p ON p.id=l.product_id "
            "WHERE p.asin=? AND l.channel='coupang' LIMIT 1", (asin,)).fetchone()
        return r

    cnt = collections.Counter()
    dup_listing_spids = []
    for i, (spid, st) in enumerate(orphans):
        if spid in known_cpids:
            cnt["이미처리됨(resume skip)"] += 1
            continue
        info = cs.get_seller_product(spid)
        d = info.get("data") if isinstance(info, dict) else None
        if not isinstance(d, dict):
            cnt["GET실패"] += 1; continue
        name = (d.get("sellerProductName") or "").strip()
        cands = idx.get(name, [])
        if not cands:
            cnt["이름매칭없음"] += 1; continue
        asins = set(a for _, a in cands if a)
        if len(asins) != 1:
            cnt["애매(다중ASIN)"] += 1; continue
        asin = next(iter(asins))
        ex = has_listing(asin)
        if ex:
            cnt["중복리스팅(P3대상)"] += 1
            dup_listing_spids.append((spid, st, asin, ex["channel_product_id"]))
            continue
        # 흡수 — 이미지 있는 product_id 선택
        pid = None
        for cid, _ in cands:
            n = con.execute("SELECT COUNT(*) c FROM image_cache WHERE product_id=?", (cid,)).fetchone()["c"]
            if n > 0:
                pid = cid; break
        pid = pid or cands[0][0]
        lstatus = STATUS_MAP.get(st, "listed")
        cnt[f"흡수가능({lstatus})"] += 1
        if apply:
            con.execute(
                "INSERT INTO listings_pa (product_id, channel, channel_product_id, status, "
                "coupang_auto_matched, coupang_seller_status, coupang_status_name, error_message) "
                "VALUES (?, 'coupang', ?, ?, 0, ?, ?, 'orphan reconcile P1 20260602')",
                (pid, spid, lstatus,
                 {"listed": "APPROVED"}.get(lstatus, ""), st))
            con.commit()
        if (i + 1) % 200 == 0:
            print(f"  ...{i+1}/{len(orphans)}", flush=True)
        time.sleep(0.12)

    # dup listing 목록 저장 (P3)
    with open("/home/ubuntu/logs/p3_dup_listings.txt", "w") as f:
        for spid, st, asin, keep_cpid in dup_listing_spids:
            f.write(f"{spid}\t{st}\t{asin}\t{keep_cpid}\n")

    print(f"\n=== P1 결과 (apply={apply}, 총 {len(orphans)}) ===")
    for k, v in cnt.most_common():
        print(f"  {k}: {v}")
    print(f"  → 중복리스팅 목록: /home/ubuntu/logs/p3_dup_listings.txt")


if __name__ == "__main__":
    main()
