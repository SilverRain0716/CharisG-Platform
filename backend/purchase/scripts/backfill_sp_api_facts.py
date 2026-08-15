"""쿠팡 listed 상품 중 sp_api_facts_json 이 비어있는 ASIN 에 대해 SP-API CatalogItems
풀 facts 를 받아 JSONL 로 적재한다 (DB 쓰기 없음 = 데일리 잡과 락 충돌 0).

설계:
  - persist=False 로 호출 → SP-API GET 만, products UPDATE 안 함.
  - 결과는 --out JSONL 에 한 줄씩 append: {"asin","ok","facts"}.
  - 재개: 기존 JSONL 에 있는 asin 은 skip (ok/실패 무관 — 죽은 ASIN 무한재시도 방지).
  - 적재 완료 후 merge_sp_api_facts.py 가 _persist_facts 로 DB 반영 (별도 단계).
  - rate limit 은 sp_api_facts._rate_limit_wait (0.5s/req) 가 내부에서 처리.

사용:
  python -m backend.purchase.scripts.backfill_sp_api_facts --limit 500 --out /tmp/facts_pilot.jsonl
  python -m backend.purchase.scripts.backfill_sp_api_facts --out /tmp/facts_backfill.jsonl   # 전체
"""
import argparse
import json
import os
import sqlite3
import sys
import time

from dotenv import load_dotenv

_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))  # systemd 아닌 직접 실행 시 SP-API 크레덴셜 주입

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"

# 용량/치수/속성 채움 측정 필드
VOL = ("net_content_ml", "net_content_g", "item_volume_ml", "unit_count_value", "number_of_items")
DIM = ("item_dimensions", "package_dimensions")
WEIGHT = ("item_weight_g", "item_display_weight_g", "package_weight_g")
ATTR = ("material", "size_attr", "flavor_attr", "item_form")


def load_done(path: str) -> set:
    done = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("ok"):  # 성공만 skip — 실패(429/일시오류)는 재개 시 재시도
                        done.add(rec["asin"])
                except Exception:
                    pass
    return done


def target_asins(limit: int, random_order: bool = False) -> list:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    order = "RANDOM()" if random_order else "p.asin"
    q = ("SELECT DISTINCT p.asin FROM products p JOIN listings_pa l ON l.product_id=p.id "
         "WHERE l.channel='coupang' AND l.status='listed' "
         "AND (p.sp_api_facts_json IS NULL OR p.sp_api_facts_json='') "
         f"AND p.asin IS NOT NULL AND p.asin!='' ORDER BY {order}")
    if limit and limit > 0:
        q += f" LIMIT {int(limit)}"
    rows = [r["asin"] for r in c.execute(q).fetchall()]
    c.close()
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0=전체")
    ap.add_argument("--out", default="/tmp/facts_backfill.jsonl")
    ap.add_argument("--random", action="store_true", help="무작위 추출 (대표샘플용)")
    args = ap.parse_args()

    from backend.purchase.services.sp_api_facts import fetch_full_catalog_facts

    done = load_done(args.out)
    asins = [a for a in target_asins(args.limit, random_order=args.random) if a not in done]
    print(f"[backfill] 대상 {len(asins):,} (이미 적재 skip {len(done):,}) → {args.out}", flush=True)

    n_ok = n_fail = 0
    stat = {"vol": 0, "dim": 0, "weight": 0, "attr": 0}
    t0 = time.time()
    with open(args.out, "a", encoding="utf-8") as out:
        for i, asin in enumerate(asins, 1):
            try:
                facts = fetch_full_catalog_facts(asin, persist=False)
            except Exception as e:
                facts = None
                print(f"  ! {asin} 예외: {e}", flush=True)
            ok = bool(facts)
            if ok:
                n_ok += 1
                if any(facts.get(k) not in (None, "", 0) for k in VOL):
                    stat["vol"] += 1
                if any(facts.get(k) not in (None, "", 0, {}) for k in DIM):
                    stat["dim"] += 1
                if any(facts.get(k) not in (None, "", 0) for k in WEIGHT):
                    stat["weight"] += 1
                if any(facts.get(k) not in (None, "", 0) for k in ATTR):
                    stat["attr"] += 1
            else:
                n_fail += 1
            out.write(json.dumps({"asin": asin, "ok": ok, "facts": facts}, ensure_ascii=False) + "\n")
            if i % 100 == 0:
                out.flush()
                rate = i / max(time.time() - t0, 1)
                eta = (len(asins) - i) / max(rate, 0.01) / 60
                print(f"  [{i:,}/{len(asins):,}] ok={n_ok:,} fail={n_fail:,} "
                      f"vol={stat['vol']:,} dim={stat['dim']:,} wt={stat['weight']:,} attr={stat['attr']:,} "
                      f"| {rate:.1f}/s ETA {eta:.0f}분", flush=True)

    n = max(n_ok, 1)
    print("=" * 70, flush=True)
    print(f"[done] ok={n_ok:,} fail={n_fail:,}", flush=True)
    print(f"  용량류(ml/g/count/pack): {stat['vol']:,} ({100*stat['vol']/n:.1f}% of ok)", flush=True)
    print(f"  치수(dimensions)       : {stat['dim']:,} ({100*stat['dim']/n:.1f}%)", flush=True)
    print(f"  무게(weight)           : {stat['weight']:,} ({100*stat['weight']/n:.1f}%)", flush=True)
    print(f"  속성(material/size/..) : {stat['attr']:,} ({100*stat['attr']/n:.1f}%)", flush=True)


if __name__ == "__main__":
    sys.exit(main())
