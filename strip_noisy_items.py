"""sellerProduct items 재구성 — noise 옵션만 drop 후 PUT.

대상: F audit 가 식별한 78 sellerProduct.
처리:
  1) get_seller_product(spid) → data.items
  2) 각 item 의 attributes 값에 is_noisy_axis_value 적용
       → 노이즈 attribute 가 있으면 그 item 전체 drop
  3) 정제된 items 로 data 갱신 + requested=True
  4) dry-run / --apply / --spid <id> 단건 / --limit N
"""
import sys, os, argparse, time, json
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.database import get_db
from backend.purchase.services.variation import is_noisy_axis_value
from backend.purchase.services.coupang_service import (
    _signature, BASE, _request_with_retry, _extract_error_messages,
    get_seller_product, stop_sales_vendor_item,
)

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--spid", default=None, help="단건 처리할 sellerProductId")
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--sleep", type=float, default=1.0)
args = ap.parse_args()

# F audit 와 동일 — 영향받은 spid 집합 추출
with get_db() as conn:
    rows = conn.execute("""
      SELECT DISTINCT l.channel_product_id spid
      FROM listing_options lo
      JOIN listings_pa l ON l.id=lo.listing_id
      WHERE l.channel='coupang' AND l.status='listed' AND lo.status='active'
        AND l.channel_product_id IS NOT NULL AND l.channel_product_id != ''
    """).fetchall()
all_spids = [r["spid"] for r in rows]

# 각 spid 에 노이즈 option 이 진짜 있는지 노이즈 평가
noisy_spids = []
with get_db() as conn:
    for spid in all_spids:
        cnt = conn.execute("""
          SELECT COUNT(*) FROM listing_options lo
          JOIN listings_pa l ON l.id=lo.listing_id
          WHERE l.channel_product_id=? AND l.channel='coupang' AND lo.status='active'
        """, (spid,)).fetchone()[0]
        if cnt == 0:
            continue
        labels = conn.execute("""
          SELECT lo.option_label FROM listing_options lo
          JOIN listings_pa l ON l.id=lo.listing_id
          WHERE l.channel_product_id=? AND l.channel='coupang' AND lo.status='active'
        """, (spid,)).fetchall()
        has_noisy = False
        for lr in labels:
            for tok in (lr["option_label"] or "").split("/"):
                if is_noisy_axis_value(tok.strip()):
                    has_noisy = True
                    break
            if has_noisy:
                break
        if has_noisy:
            noisy_spids.append(spid)

print(f"[F audit 일치] noisy sellerProduct {len(noisy_spids):,}건")

# 필터
target_spids = noisy_spids
if args.spid:
    target_spids = [args.spid]
elif args.limit > 0:
    target_spids = target_spids[:args.limit]
print(f"[처리 대상] {len(target_spids):,}건 apply={args.apply}")

_AXIS_NAME_HINTS = (
    "색상", "사이즈", "스타일", "맛", "향", "크기", "패션의류",
    "신발사이즈", "color", "size", "style", "flavor", "scent",
)

def _is_axis_attr(name: str) -> bool:
    n = (name or "").lower()
    return any(h.lower() in n for h in _AXIS_NAME_HINTS)

def is_item_noisy(item: dict) -> tuple[bool, list[str]]:
    """item 의 옵션 axis attribute 값 또는 itemName 에 noise 있으면 True.

    - axis attribute 만 검사 (방수/구성요소 같은 메타는 무시)
    - 빈값 무시 (단순 정보 누락은 노이즈 아님)
    """
    noise_hits = []
    for a in (item.get("attributes") or []):
        name = a.get("attributeTypeName") or ""
        val = (a.get("attributeValueName") or "").strip()
        if not val:
            continue
        if not _is_axis_attr(name):
            continue
        if is_noisy_axis_value(val):
            noise_hits.append(f"{name}={val!r}")
    # itemName 토큰 검사 — 빈 토큰 제외
    name = item.get("itemName") or ""
    for tok in name.split("/"):
        t = tok.strip()
        if t and is_noisy_axis_value(t):
            noise_hits.append(f"itemName_token={t!r}")
            break
    return (len(noise_hits) > 0, noise_hits)

PUT_PATH = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
ok = changed = unchanged = fail = 0
all_drop_skip = 0

for spid in target_spids:
    info = get_seller_product(str(spid))
    if not info or not isinstance(info.get("data"), dict):
        fail += 1
        print(f"  FAIL spid={spid}: 조회 실패")
        time.sleep(args.sleep)
        continue
    data = info["data"]
    items = data.get("items") or []
    if not items:
        fail += 1
        print(f"  FAIL spid={spid}: items 없음")
        time.sleep(args.sleep)
        continue

    keep = []
    drops = []
    for it in items:
        noisy, hits = is_item_noisy(it)
        if noisy:
            drops.append((it.get("vendorItemId"), it.get("itemName"), hits[:3]))
        else:
            keep.append(it)

    if not drops:
        unchanged += 1
        print(f"  SAME spid={spid}: 노이즈 item 0 (변경 불필요)")
        time.sleep(args.sleep)
        continue

    if not keep:
        # 모든 item 이 노이즈 — PUT 으로 0 items 보내면 거절. skip.
        all_drop_skip += 1
        print(f"  ALL-NOISE spid={spid}: items 전부 노이즈, PUT 보류 (stop_sales 별도 검토)")
        for d in drops[:5]:
            print(f"    drop vid={d[0]} name='{(d[1] or '')[:50]}' hits={d[2]}")
        time.sleep(args.sleep)
        continue

    print(f"  spid={spid}: keep {len(keep)} / drop {len(drops)}")
    for d in drops[:5]:
        print(f"    drop vid={d[0]} name='{(d[1] or '')[:50]}' hits={d[2]}")

    if not args.apply:
        time.sleep(args.sleep)
        continue

    # 노이즈 vendor-item 만 stop_sales (개별 정지) — PUT 없음 = 재심사 없음
    per_ok = per_fail = 0
    for vid, name, hits in drops:
        if not vid:
            per_fail += 1
            print(f"    SKIP no vid (name='{name}')")
            continue
        try:
            success, msg = stop_sales_vendor_item(str(vid))
            if success:
                per_ok += 1
                print(f"    STOP OK vid={vid}")
            else:
                per_fail += 1
                print(f"    STOP FAIL vid={vid}: {msg[:120]}")
        except Exception as e:
            per_fail += 1
            print(f"    STOP EX vid={vid}: {e}")
        time.sleep(0.4)
    if per_ok > 0 and per_fail == 0:
        ok += 1
        changed += 1
    elif per_ok > 0:
        changed += 1
        ok += 1
    else:
        fail += 1
    print(f"    spid={spid} 결과: stop ok={per_ok} fail={per_fail}")
    time.sleep(args.sleep)

print(f"\n[총: ok {ok} / unchanged {unchanged} / all-noise-skip {all_drop_skip} / fail {fail}]")
