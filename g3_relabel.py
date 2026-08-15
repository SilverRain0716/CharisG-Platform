"""G3 — partial 옵션 라벨 cleaning 후 PUT 재구성.

각 sellerProduct 의 items[].attributes 를 clean_axis_value 결과로 갱신.
중복 dedupe + 옵션 중복 발생 시 skip.

위험: 콘텐츠 수정 = 4~24h 재심사 + ID 분리 위험 (D 시범에선 ID 유지 확인됨).
사용:
  python g3_relabel.py --spid 16232344531              # dry-run 1건
  python g3_relabel.py --apply --spid 16232344531      # 실 PUT 1건
  python g3_relabel.py --apply --limit 5               # 5건 시범
  python g3_relabel.py --apply                         # 전체
"""
import sys, os, sqlite3, argparse, time
os.chdir("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")
from backend.purchase.services.variation import clean_axis_value
from backend.purchase.services.coupang_service import (
    _signature, BASE, _request_with_retry, _extract_error_messages,
    get_seller_product,
)

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--spid", default=None)
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--sleep", type=float, default=1.0)
args = ap.parse_args()

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
conn = sqlite3.connect(DB, timeout=180)
conn.row_factory = sqlite3.Row

# partial spids 식별 (preview_partial 와 동일)
from collections import defaultdict
rows = conn.execute("""
  SELECT lo.option_label, l.channel_product_id spid
  FROM listing_options lo
  JOIN listings_pa l ON l.id=lo.listing_id
  WHERE l.channel='coupang' AND l.status='listed' AND lo.status='active'
    AND l.channel_product_id IS NOT NULL AND l.channel_product_id != ''
""").fetchall()
spids = set()
for r in rows:
    tokens = [t.strip() for t in (r["option_label"] or "").split("/") if t.strip()]
    if not tokens:
        continue
    cleaned = [clean_axis_value(t) for t in tokens]
    if any(c is None for c in cleaned) or any(c != t for c, t in zip(cleaned, tokens) if c):
        spids.add(r["spid"])

targets = list(spids)
if args.spid:
    targets = [args.spid] if args.spid in spids else []
elif args.limit > 0:
    targets = sorted(targets)[:args.limit]

print(f"[대상] {len(targets)} sellerProduct  apply={args.apply}")

_AXIS_NAME_HINTS = ("색상","사이즈","스타일","맛","향","크기","패션의류","신발사이즈",
                    "color","size","style","flavor","scent")

def _is_axis_attr(name: str) -> bool:
    n = (name or "").lower()
    return any(h.lower() in n for h in _AXIS_NAME_HINTS)

def clean_item(item: dict) -> dict | None:
    """item.attributes 의 axis 값 cleaning. None 반환 = drop (모든 axis 무효).

    Returns 수정된 item 또는 None (drop).
    """
    new_attrs = []
    changed = False
    for a in (item.get("attributes") or []):
        name = a.get("attributeTypeName") or ""
        val = (a.get("attributeValueName") or "").strip()
        if not val or not _is_axis_attr(name):
            new_attrs.append(a)
            continue
        cleaned = clean_axis_value(val)
        if cleaned is None:
            # axis 값이 노이즈만 — 옵션 전체 의미 없음 → item drop 신호
            return None
        if cleaned != val:
            changed = True
        new_a = dict(a)
        new_a["attributeValueName"] = cleaned[:24]
        new_attrs.append(new_a)
    if not changed:
        return item  # 변경 없음
    new_item = dict(item)
    new_item["attributes"] = new_attrs
    return new_item

PUT_PATH = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
ok = unchanged = put_fail = no_change_skip = 0

for spid in targets:
    info = get_seller_product(str(spid))
    if not info or not isinstance(info.get("data"), dict):
        put_fail += 1
        print(f"  FAIL spid={spid}: 조회 실패")
        time.sleep(args.sleep)
        continue
    data = info["data"]
    items = data.get("items") or []
    if not items:
        put_fail += 1
        print(f"  FAIL spid={spid}: items 없음")
        time.sleep(args.sleep)
        continue

    new_items = []
    drops = []
    for it in items:
        ci = clean_item(it)
        if ci is None:
            drops.append((it.get("vendorItemId"), it.get("itemName")))
        else:
            new_items.append(ci)

    # 옵션 중복 (attributes 시그니처) 검사
    sigs = [tuple(sorted((a.get("attributeTypeName"),
                          a.get("attributeValueName") or "")
                         for a in (ni.get("attributes") or [])))
            for ni in new_items]
    dup = len(sigs) != len(set(sigs))
    if dup:
        no_change_skip += 1
        print(f"  SKIP spid={spid}: cleaned 옵션 중복 발생 → PUT 보류")
        time.sleep(args.sleep)
        continue

    # 변경 여부 — 모든 item 그대로면 PUT 안 함
    changed = any(any((a.get("attributeValueName") != b.get("attributeValueName"))
                       for a, b in zip(old.get("attributes") or [], new.get("attributes") or []))
                  for old, new in zip(items, new_items)
                  if old.get("vendorItemId") == new.get("vendorItemId"))
    if not changed and not drops:
        unchanged += 1
        print(f"  SAME spid={spid}: 변경 불필요")
        time.sleep(args.sleep)
        continue

    if drops:
        print(f"  spid={spid}: drop {len(drops)}건 — PUT 으로 drop 불가 (판매중지 후 별도)")
        for vid, name in drops[:3]:
            print(f"    drop vid={vid} name='{(name or '')[:50]}'")
        # drop 은 PUT 으로 안 됨 (판매중 item 삭제 거부). PUT skip + stop_sales 권장.
        no_change_skip += 1
        time.sleep(args.sleep)
        continue

    print(f"  spid={spid}: items {len(items)} cleaning 변경 적용")
    for old, new in zip(items, new_items):
        old_attrs = {a.get("attributeTypeName"): a.get("attributeValueName") for a in (old.get("attributes") or [])}
        new_attrs = {a.get("attributeTypeName"): a.get("attributeValueName") for a in (new.get("attributes") or [])}
        for k in old_attrs:
            if old_attrs[k] != new_attrs.get(k):
                print(f"    vid={old.get('vendorItemId')} {k}: '{old_attrs[k]}' → '{new_attrs.get(k)}'")
                break

    if not args.apply:
        time.sleep(args.sleep)
        continue

    # PUT
    data["items"] = new_items
    data["requested"] = True
    try:
        r = _request_with_retry("PUT", BASE + PUT_PATH,
                                 headers=_signature("PUT", PUT_PATH),
                                 json=data, timeout=30)
        if r is None:
            put_fail += 1
            print(f"    PUT FAIL spid={spid}: no response")
            time.sleep(args.sleep)
            continue
        body = r.json() if r.text else {}
        if not (r.status_code < 400 and isinstance(body, dict) and body.get("code") != "ERROR"):
            msgs = _extract_error_messages(body)
            put_fail += 1
            print(f"    PUT FAIL spid={spid}: status={r.status_code} {('; '.join(msgs))[:150]}")
        else:
            ok += 1
            print(f"    PUT OK spid={spid}")
    except Exception as e:
        put_fail += 1
        print(f"    PUT EX spid={spid}: {e}")
    time.sleep(args.sleep)

print(f"\n[총: ok {ok} / unchanged {unchanged} / skip {no_change_skip} / fail {put_fail}]")
conn.close()
