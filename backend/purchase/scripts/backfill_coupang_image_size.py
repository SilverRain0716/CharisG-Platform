"""쿠팡 이미지 규격 백필 — 기존 image_cache 파일 중 단변<500(또는 5000 초과) 비규격
이미지를 흰배경 1000x1000 으로 재보정(in-place). 재등록 대비.

근본 버그: image_downloader 가 단변<500 비율 이미지를 그대로 저장 → 쿠팡 승인반려.
신규는 image_downloader._normalize_for_coupang 로 해결됐고, 이 스크립트는 이미
저장된 비규격 파일을 동일 로직으로 고친다.

사용:
  # 전체 listed 쿠팡 중 비규격만 집계(읽기전용)
  python -m backend.purchase.scripts.backfill_coupang_image_size --dry-run --scope listed
  # 특정 product_id 들만 실제 보정
  python -m backend.purchase.scripts.backfill_coupang_image_size --product-ids 123,456
  # 전체 listed 비규격 실제 보정
  python -m backend.purchase.scripts.backfill_coupang_image_size --scope listed --apply
"""
import argparse
import os
import sqlite3
import sys

from dotenv import load_dotenv
load_dotenv()  # 단발 스크립트 — COUPANG/SP 가드용 env 명시 로드 (feedback_pa_manual_script_dotenv)

from PIL import Image
from backend.purchase.services.image_downloader import _normalize_for_coupang

DB_PATH = os.environ.get(
    "PA_DB_PATH",
    str(os.path.join(os.path.dirname(__file__), "..", "purchase.db")),
)


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=180000")
    return c


def _target_product_ids(con, scope, product_ids):
    if product_ids:
        return [int(x) for x in product_ids.split(",") if x.strip()]
    if scope == "listed":
        rows = con.execute(
            "SELECT DISTINCT product_id FROM listings_pa "
            "WHERE channel='coupang' AND status='listed'"
        ).fetchall()
        return [r["product_id"] for r in rows]
    # scope == all: image_cache 전체
    rows = con.execute("SELECT DISTINCT product_id FROM image_cache").fetchall()
    return [r["product_id"] for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["listed", "all"], default="listed")
    ap.add_argument("--product-ids", default="")
    ap.add_argument("--apply", action="store_true", help="실제 파일 수정 (없으면 dry-run)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    con = _conn()
    pids = _target_product_ids(con, args.scope, args.product_ids)
    print(f"대상 product_id: {len(pids)} (scope={args.scope}, apply={apply})")

    seen = fixed = noncompliant = missing = err = already = 0
    for i, pid in enumerate(pids):
        if args.limit and seen >= args.limit:
            break
        rows = con.execute(
            "SELECT id, local_path FROM image_cache WHERE product_id=?", (pid,)
        ).fetchall()
        for r in rows:
            seen += 1
            lp = r["local_path"]
            if not lp or not os.path.isfile(lp):
                missing += 1
                continue
            try:
                with Image.open(lp) as im:
                    w, h = im.size
            except Exception:
                err += 1
                continue
            # 쿠팡 규격: 양변 ≥500, ≤5000. 위반 시에만 보정.
            if min(w, h) >= 500 and max(w, h) <= 5000:
                already += 1
                continue
            noncompliant += 1
            if apply:
                try:
                    with Image.open(lp) as im:
                        out = _normalize_for_coupang(im)
                    out.save(lp, "JPEG", quality=85, optimize=True)
                    con.execute(
                        "UPDATE image_cache SET size_bytes=? WHERE id=?",
                        (os.path.getsize(lp), r["id"]),
                    )
                    con.commit()
                    fixed += 1
                except Exception as e:
                    err += 1
                    print(f"  보정 실패 pid={pid} {lp}: {e}", file=sys.stderr)
        if (i + 1) % 2000 == 0:
            print(f"  ...{i+1}/{len(pids)} seen={seen} 비규격={noncompliant} fixed={fixed}",
                  flush=True)

    print("\n=== 결과 ===")
    print(f"  이미지 검사: {seen}")
    print(f"  규격OK(스킵): {already}")
    print(f"  파일없음: {missing}, 열기실패: {err}")
    print(f"  비규격(단변<500 또는 >5000): {noncompliant}")
    print(f"  {'실제 보정' if apply else 'DRY-RUN(보정 안 함)'}: {fixed if apply else 0}")
    if not apply and noncompliant:
        print("  → 실제 보정하려면 --apply 추가")


if __name__ == "__main__":
    main()
