"""승인반려(이미지 규격) 상품 복구 — 이미지 1000x1000 재패딩 후 쿠팡 재제출.

흐름(상품별): channel_product_id→product_id 매핑 → (apply 시) 비규격 이미지 in-place
재패딩 → _get_product_images 로 교정 URL → coupang_service.relist_with_fixed_images
로 GET→images 교체→requested=True→PUT.

  # 드라이런(읽기전용: GET + 페이로드 미리보기만, 파일/ PUT 변경 없음)
  python -m backend.purchase.scripts.relist_rejected_images --from-file /home/ubuntu/logs/all_rejected_spids.txt --dry-run --limit 5
  # 실제 복구 (파일 재패딩 + 쿠팡 PUT)
  python -m backend.purchase.scripts.relist_rejected_images --spids 16235599425,16234866051 --apply --sleep 1.0
"""
import argparse
import os
import sqlite3
import sys
import time

from dotenv import load_dotenv
load_dotenv()  # 단발 스크립트 env 명시 로드 (feedback_pa_manual_script_dotenv)

from PIL import Image
from backend.purchase.services import coupang_service as cs
from backend.purchase.services.coupang_lister import _get_product_images
from backend.purchase.services.image_downloader import _normalize_for_coupang

DB_PATH = os.environ.get(
    "PA_DB_PATH",
    str(os.path.join(os.path.dirname(__file__), "..", "purchase.db")),
)


def _repad_product_images(con, product_id):
    """product_id 의 비규격(단변<500 또는 >5000) 이미지를 1000x1000 으로 in-place 재패딩."""
    fixed = 0
    rows = con.execute(
        "SELECT id, local_path FROM image_cache WHERE product_id=?", (product_id,)
    ).fetchall()
    for r in rows:
        lp = r["local_path"]
        if not lp or not os.path.isfile(lp):
            continue
        try:
            with Image.open(lp) as im:
                w, h = im.size
            if min(w, h) >= 500 and max(w, h) <= 5000:
                continue
            with Image.open(lp) as im:
                out = _normalize_for_coupang(im)
            out.save(lp, "JPEG", quality=85, optimize=True)
            con.execute("UPDATE image_cache SET size_bytes=? WHERE id=?",
                        (os.path.getsize(lp), r["id"]))
            con.commit()
            fixed += 1
        except Exception as e:
            print(f"    재패딩 실패 pid={product_id} {lp}: {e}", file=sys.stderr)
    return fixed


def _load_spids(args):
    spids = []
    if args.spids:
        spids = [s.strip() for s in args.spids.split(",") if s.strip()]
    elif args.from_file:
        with open(args.from_file) as f:
            for line in f:
                p = line.strip().split("\t")
                if p and p[0]:
                    spids.append(p[0])
    return spids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spids", default="")
    ap.add_argument("--from-file", default="")
    ap.add_argument("--apply", action="store_true", help="실제 재패딩 + 쿠팡 PUT")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=1.0, help="상품 간 간격(초) — rate limit 보호")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    spids = _load_spids(args)
    if args.limit:
        spids = spids[: args.limit]
    print(f"대상 spid: {len(spids)} (apply={apply}, sleep={args.sleep})")

    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=180000")

    n_ok = n_fail = n_unmapped = n_noimg = 0
    for i, spid in enumerate(spids):
        row = con.execute(
            "SELECT product_id FROM listings_pa WHERE channel='coupang' AND channel_product_id=? LIMIT 1",
            (spid,)).fetchone()
        if not row:
            n_unmapped += 1
            print(f"  [{spid}] SKIP 미매핑")
            continue
        pid = row["product_id"]
        if apply:
            nf = _repad_product_images(con, pid)
            if nf:
                print(f"  [{spid}] pid={pid} 이미지 {nf}장 재패딩")
        urls = _get_product_images(pid)
        if not urls:
            n_noimg += 1
            print(f"  [{spid}] SKIP 교정이미지 없음 (백필/재다운로드 필요)")
            continue
        ok, msg = cs.relist_with_fixed_images(spid, urls, dry_run=not apply)
        if ok:
            n_ok += 1
            print(f"  [{spid}] {'PUT성공' if apply else 'DRY'} — {msg}")
        else:
            n_fail += 1
            print(f"  [{spid}] FAIL — {msg}")
        if apply and args.sleep:
            time.sleep(args.sleep)

    print("\n=== 결과 ===")
    print(f"  {'재제출 성공' if apply else 'DRY 통과'}: {n_ok}")
    print(f"  실패: {n_fail}, 미매핑: {n_unmapped}, 교정이미지없음: {n_noimg}")


if __name__ == "__main__":
    main()
