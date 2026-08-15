"""backfill_sp_api_facts.py 가 적재한 JSONL 을 products 테이블에 반영한다.

_persist_facts 와 동일한 COALESCE 로직(기존 값 있으면 보존):
  sp_api_facts_json / sp_api_facts_at = 항상 갱신
  parent_asin / weight_g / brand / description_en / images_json = 비어있을 때만 채움

단일 커넥션 + 배치 커밋(기본 500)으로 데일리 잡과의 락 충돌 최소화.
한가한 시간대에 실행 권장. --apply 없으면 dry-run(카운트만).

사용:
  python -m backend.purchase.scripts.merge_sp_api_facts --inp /tmp/facts_backfill.jsonl          # dry-run
  python -m backend.purchase.scripts.merge_sp_api_facts --inp /tmp/facts_backfill.jsonl --apply
"""

# .env auto-load — 단발 실행 시 COUPANG_*/AMZ_* 환경변수 보장
from dotenv import load_dotenv
from pathlib import Path as _Path
load_dotenv(_Path(__file__).resolve().parents[3] / ".env")

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"

UPDATE = """UPDATE products SET
      sp_api_facts_json = ?,
      sp_api_facts_at = ?,
      parent_asin   = COALESCE(parent_asin, ?),
      weight_g      = COALESCE(weight_g, ?),
      brand         = COALESCE(NULLIF(brand, ''), ?),
      description_en= COALESCE(NULLIF(description_en, ''), ?),
      images_json   = COALESCE(NULLIF(images_json, ''), NULLIF(images_json, '[]'), ?)
   WHERE asin = ?"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inp", default="/tmp/facts_backfill.jsonl")
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB, timeout=180)
    conn.execute("PRAGMA busy_timeout=180000")
    conn.execute("PRAGMA journal_mode=WAL")

    n_rec = n_skip = rows_upd = pending = 0
    t0 = time.time()
    with open(args.inp, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not rec.get("ok") or not rec.get("facts"):
                n_skip += 1
                continue
            fa = rec["facts"]
            asin = rec["asin"]
            weight_g = fa.get("item_weight_g") or fa.get("item_display_weight_g") or fa.get("net_content_g")
            parent = fa.get("parent_asin")
            brand = fa.get("brand")
            desc = fa.get("description_en")
            if not desc and fa.get("bullet_points"):
                desc = "\n".join(f"• {b}" for b in fa["bullet_points"])
            images = fa.get("images") or []
            images_json = json.dumps(images, ensure_ascii=False) if images else None
            facts_json = json.dumps(fa, ensure_ascii=False)
            facts_at = fa.get("fetched_at") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            n_rec += 1
            if args.apply:
                cur = conn.execute(UPDATE, (facts_json, facts_at, parent, weight_g, brand, desc, images_json, asin))
                rows_upd += cur.rowcount
                pending += 1
                if pending >= args.batch:
                    conn.commit()
                    pending = 0
                    print(f"  …{n_rec:,} 반영 (rows {rows_upd:,})", flush=True)
    if args.apply:
        conn.commit()
    conn.close()
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] facts 레코드 {n_rec:,} (skip {n_skip:,}) → products 행 갱신 {rows_upd:,} "
          f"| {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    sys.exit(main())
