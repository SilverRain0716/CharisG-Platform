"""활성 주문 일회성 reconciliation — 채널의 현재 상태로 우리 단계 정리.

배경:
  새 폴러 가동 전에 발생한 변경 이벤트는 네이버 last-changed-statuses 의 24h
  검색 범위 밖이라 자동 advance 불가. 김희선(2026050560391231) 처럼 실제로
  CANCELED 인데 우리 DB 가 order_received 인 케이스 정리용.

대상:
  orders WHERE current_step NOT IN ('completed', 'cancelled')

동작:
  - smartstore: get_product_order_details(POIDs) batch — productOrderStatus 직접 GET
  - coupang: 활성 주문의 placed_at 범위로 status 별 get_orders 후 orderId 매칭
  - 매핑 함수로 forward-only advance

사용법:
  python3 scripts/reconcile_orders.py --dry-run
  python3 scripts/reconcile_orders.py --apply [--channel smartstore|coupang|all]
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

with open(".env") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from backend.purchase.services.naver_commerce_service import get_product_order_details
from backend.purchase.services.coupang_service import get_orders as cp_get_orders
from backend.purchase.services.channel_step_mapper import map_channel_to_step
from backend.purchase.services.order_receiver_service import advance_step

DB_HOT = "backend/purchase/purchase_hot.db"
DB_COLD = "backend/purchase/purchase.db"
KST = timezone(timedelta(hours=9))


def collect_active(channel: str = None):
    conn = sqlite3.connect(DB_HOT)
    conn.row_factory = sqlite3.Row
    where = ["current_step NOT IN ('completed', 'cancelled')"]
    params = []
    if channel:
        where.append("channel = ?")
        params.append(channel)
    rows = conn.execute(
        f"""SELECT id, channel, channel_order_id, customer_name, current_step,
                   placed_at
            FROM orders WHERE {' AND '.join(where)} ORDER BY id""",
        params,
    ).fetchall()
    conn.close()
    return rows


def reconcile_smartstore(rows, apply_changes: bool):
    if not rows:
        print("[smartstore] 활성 주문 없음")
        return
    poids = [str(r["channel_order_id"]) for r in rows]
    print(f"[smartstore] {len(poids)} 건 일괄 GET ...")
    details = get_product_order_details(poids) or []

    by_poid = {}
    for d in details:
        po = d.get("productOrder") or {}
        poid = po.get("productOrderId")
        if poid:
            by_poid[str(poid)] = po

    advanced = 0
    same = 0
    no_resp = 0
    for r in rows:
        poid = str(r["channel_order_id"])
        po = by_poid.get(poid)
        if not po:
            no_resp += 1
            print(f"  · #{r['id']} {poid} {r['customer_name']} → 응답 없음 (외부 삭제 가능)")
            continue
        ext_status = po.get("productOrderStatus")
        new_step = map_channel_to_step("smartstore", ext_status, r["current_step"])
        if not new_step:
            same += 1
            continue
        msg = f"smartstore:{ext_status}"
        if apply_changes:
            ok = advance_step(r["id"], new_step, note=msg)
            mark = "✓" if ok else "✗"
        else:
            mark = "→"
        print(f"  {mark} #{r['id']} {poid} {r['customer_name']}  {r['current_step']} → {new_step}  ({ext_status})")
        if apply_changes:
            advanced += 1

    print(f"[smartstore] {advanced} advance / {same} same / {no_resp} no-response")


def reconcile_coupang(rows, apply_changes: bool):
    if not rows:
        print("[coupang] 활성 주문 없음")
        return
    # placed_at 범위 → createdAtFrom/To (최소~최대)
    dates = []
    for r in rows:
        if r["placed_at"]:
            try:
                dt = datetime.fromisoformat(str(r["placed_at"]).replace("Z", "+00:00"))
                dates.append(dt.astimezone(KST))
            except Exception:
                pass
    if not dates:
        print("[coupang] placed_at 없음 — 스킵")
        return
    start = min(dates).strftime("%Y-%m-%d")
    end = (max(dates) + timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"[coupang] 활성 {len(rows)}건  검색 범위 {start} ~ {end}")

    target_oids = {str(r["channel_order_id"]): r for r in rows}

    # 쿠팡 placedStatus valid: ACCEPT/INSTRUCT/DEPARTURE/DELIVERING/FINAL_DELIVERY/NONE_TRACKING
    STATUSES = ["ACCEPT", "INSTRUCT", "DEPARTURE", "DELIVERING", "FINAL_DELIVERY", "NONE_TRACKING"]
    PRIORITY = {s: i for i, s in enumerate(STATUSES)}
    final_status: dict[str, str] = {}
    for st in STATUSES:
        sheets = cp_get_orders(start, end, st) or []
        for sh in sheets:
            oid = sh.get("orderId") or sh.get("orderSheetId")
            if oid is None:
                continue
            oid_s = str(oid)
            if oid_s in target_oids:
                if oid_s not in final_status or PRIORITY[st] > PRIORITY[final_status[oid_s]]:
                    final_status[oid_s] = st

    advanced = 0
    same = 0
    no_resp = 0

    for oid_s, r in target_oids.items():
        ext_status = final_status.get(oid_s)
        if not ext_status:
            no_resp += 1
            print(f"  · #{r['id']} {oid_s} {r['customer_name']} → 응답 없음")
            continue
        new_step = map_channel_to_step("coupang", ext_status, r["current_step"])
        if not new_step:
            same += 1
            continue
        if apply_changes:
            ok = advance_step(r["id"], new_step, note=f"coupang:{ext_status}")
            mark = "✓" if ok else "✗"
        else:
            mark = "→"
        print(f"  {mark} #{r['id']} {oid_s} {r['customer_name']}  {r['current_step']} → {new_step}  ({ext_status})")
        if apply_changes:
            advanced += 1

    print(f"[coupang] {advanced} advance / {same} same / {no_resp} no-response")


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    p.add_argument("--channel", choices=["smartstore", "coupang", "all"], default="all")
    args = p.parse_args()

    apply_changes = bool(args.apply)
    print(f"=== Reconciliation  channel={args.channel}  mode={'APPLY' if apply_changes else 'DRY-RUN'} ===")

    if args.channel in ("all", "smartstore"):
        reconcile_smartstore(collect_active("smartstore"), apply_changes)
        print()
    if args.channel in ("all", "coupang"):
        reconcile_coupang(collect_active("coupang"), apply_changes)


if __name__ == "__main__":
    main()
