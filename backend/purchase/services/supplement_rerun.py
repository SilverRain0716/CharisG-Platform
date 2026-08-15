"""쿠팡 영양제 listed 전수 재등록 — 잘못된 카테고리 → 식품>건강식품 leaf.

흐름:
  1. product_keywords 기반 영양제 product 식별
  2. 정책 위반 sellerProductId list 로드 (Excel) → 제외
  3. listings_pa.coupang_category_code = 매핑된 leaf code 로 UPDATE
  4. delete_product (쿠팡 영구 삭제 — 한도 회전)
  5. listings_pa.status='pending' reset (channel_product_id NULL)
  6. coupang_lister.list_product 재호출 (자동 attributes 채움 + 새 카테고리)

Discord 알림은 caller 가.
"""
import asyncio
import logging
from typing import Optional

from backend.purchase.database import get_db
from backend.purchase.services.coupang_service import stop_sales as cou_stop_sales
from backend.purchase.services.coupang_lister import list_product

logger = logging.getLogger(__name__)


# sourcing keyword → 쿠팡 식품>건강식품 leaf code
SUPPLEMENT_CATEGORY_MAP = {
    'magnesium glycinate supplement': 58931,   # 마그네슘
    'zinc supplement':                 58930,  # 아연
    'iron supplement for women':       58922,  # 철분
    'omega 3 fish oil supplement':     73134,  # 오메가3,6,9
    'krill oil supplement':           112307,  # 크릴오일
    'probiotic supplement':            58991,  # 유산균
    'digestive enzyme supplement':    102523,  # 효소
    'elderberry supplement':          102532,  # 삼부커스 (elderberry)
    'biotin supplement':               73132,  # 바이오틴
    'hair skin nails supplement':     102534,  # 허브/기타식물추출물
    'collagen gummies':                59163,  # 콜라겐/히알루론산
    'coq10 supplement':                58972,  # 코엔자임Q10/코큐텐 (소문자 저장됨)
}


def find_supplement_targets(blocklist_seller_pids: set[str] | None = None) -> list[dict]:
    """재등록 대상 영양제 listed 식별 (매출 발생 product 자동 보호).

    blocklist_seller_pids: 쿠팡 정책 위반 sellerProductId set (제외 대상).
    매출 보호: hot.orders + backup DB 매칭 product 는 자동 제외.
    """
    import os as _os
    import sqlite3 as _sq
    blocklist = blocklist_seller_pids or set()

    # 매출 발생 product_id 수집 (hot.orders + backup DB)
    sold_pids: set[int] = set()
    HOT_DB = '/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase_hot.db'
    if _os.path.exists(HOT_DB):
        try:
            h = _sq.connect(HOT_DB)
            sold_pids.update(
                r[0] for r in h.execute(
                    "SELECT DISTINCT product_id FROM orders WHERE product_id IS NOT NULL"
                ).fetchall()
            )
            h.close()
        except Exception as e:
            logger.warning(f"[supp-rerun] hot.orders 조회 실패: {e}")
    if _os.path.exists(BACKUP_DB := "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db.bak.before_c1.20260428_004801"):
        try:
            b = _sq.connect(BACKUP_DB)
            sold_pids.update(
                r[0] for r in b.execute(
                    "SELECT DISTINCT product_id FROM orders WHERE product_id IS NOT NULL"
                ).fetchall()
            )
            b.close()
        except Exception as e:
            logger.warning(f"[supp-rerun] backup orders 조회 실패: {e}")
    logger.info(f"[supp-rerun] 매출 보호 product_id: {len(sold_pids)}건")

    keywords = list(SUPPLEMENT_CATEGORY_MAP.keys())
    ph = ','.join(['?'] * len(keywords))

    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT l.product_id, l.channel_product_id, pk.keyword
                FROM listings_pa l
                JOIN product_keywords pk ON pk.product_id = l.product_id
                WHERE l.channel='coupang' AND l.status='listed'
                  AND l.channel_product_id IS NOT NULL
                  AND pk.keyword IN ({ph})
                  AND pk.is_primary = 1""",
            keywords,
        ).fetchall()

    targets = []
    for r in rows:
        if r["product_id"] in sold_pids:
            continue  # 매출 보호
        if str(r["channel_product_id"]) in blocklist:
            continue
        cat = SUPPLEMENT_CATEGORY_MAP.get(r["keyword"])
        if not cat:
            continue
        targets.append({
            "product_id": r["product_id"],
            "channel_product_id": str(r["channel_product_id"]),
            "category_code": cat,
            "keyword": r["keyword"],
        })
    return targets


def update_category_codes(targets: list[dict]) -> int:
    """listings_pa.coupang_category_code 일괄 UPDATE."""
    if not targets:
        return 0
    with get_db() as conn:
        for t in targets:
            conn.execute(
                """UPDATE listings_pa SET coupang_category_code=?
                   WHERE product_id=? AND channel='coupang'""",
                (t["category_code"], t["product_id"]),
            )
    return len(targets)


async def stop_only(targets: list[dict]) -> dict:
    """A안 — stop_sales 만 진행. 재등록 안 함.

    각 건당:
      1. cou_stop_sales(channel_product_id) — vendorItem 일괄 SUSPENSION
      2. listings_pa: status='rotated' (channel_product_id 보존 — wing 에서 동일 상품 정리 가능)
    """
    ok = 0
    fail = 0
    fail_details = []
    sem = asyncio.Semaphore(1)

    async def _stop_one(t: dict):
        nonlocal ok, fail
        async with sem:
            success, err = await asyncio.to_thread(cou_stop_sales, t["channel_product_id"])
        if success:
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa
                       SET status='rotated',
                           error_message='supplement rerun — wing 정리 대기',
                           last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='coupang'""",
                    (t["product_id"],),
                )
            ok += 1
        else:
            fail += 1
            fail_details.append({"pid": t["product_id"], "cpid": t["channel_product_id"], "err": err[:120]})
            logger.warning(f"[supp-rerun] stop fail pid={t['product_id']}: {err[:120]}")
        await asyncio.sleep(0.3)

    for t in targets:
        await _stop_one(t)
    return {"ok": ok, "fail": fail, "fail_details": fail_details}


async def stop_and_reset(targets: list[dict]) -> dict:
    """target 의 listed 상품 stop_sales (판매중지) → status='pending' reset.

    쿠팡은 '판매중' 상품 delete 불가 ('임시저장'/'저장중' 만 가능). 그래서 stop_sales 로
    SUSPENSION 처리 → 기존 row 의 channel_product_id=NULL 로 비워 재등록 가능 상태로.

    각 건당:
      1. stop_sales(channel_product_id) — vendorItem 일괄 판매 중지
      2. listings_pa: status='pending', channel_product_id=NULL, error_message=NULL
         (기존 sellerProductId 는 SUSPENSION 상태로 쿠팡에 남음)
    """
    ok = 0
    fail = 0
    fail_details = []

    sem = asyncio.Semaphore(1)

    async def _stop_one(t: dict):
        nonlocal ok, fail
        async with sem:
            success, err = await asyncio.to_thread(cou_stop_sales, t["channel_product_id"])
        if success:
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa
                       SET status='pending',
                           channel_product_id=NULL,
                           error_message=NULL,
                           last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='coupang'""",
                    (t["product_id"],),
                )
            ok += 1
        else:
            fail += 1
            fail_details.append({"pid": t["product_id"], "cpid": t["channel_product_id"], "err": err[:120]})
            logger.warning(f"[supp-rerun] stop fail pid={t['product_id']}: {err[:120]}")
        await asyncio.sleep(0.3)

    for t in targets:
        await _stop_one(t)

    return {"ok": ok, "fail": fail, "fail_details": fail_details}


# 호환 alias
delete_and_reset = stop_and_reset


def fill_default_options(product_id: int) -> None:
    """제목 정규식 추출 + default 값으로 products.coupang_attributes_json 강제 채움.

    이게 채워져 있으면 build_required_attributes 가 saved_values 우선 사용 → AI fallback 안 거치고 통과.
    옵션 정보 부족한 영양제도 일단 등록 가능 → user 가 wing 에서 수정.
    """
    import re
    import json
    PAT_CAP = re.compile(r'(\d+)\s*(정|캡슐|알|tablets?|capsules?|caps|tabs?|gummies|gummy|회분|servings?|소프트젤|softgels?)', re.IGNORECASE)
    PAT_WEIGHT = re.compile(r'(\d+(?:\.\d+)?)\s*(mg|g)\b', re.IGNORECASE)
    PAT_VOLUME = re.compile(r'(\d+(?:\.\d+)?)\s*(ml|oz)\b', re.IGNORECASE)
    PAT_PACK = re.compile(r'(\d+)\s*(개입|팩|set|pack)s?', re.IGNORECASE)

    with get_db() as conn:
        p = conn.execute('SELECT title_ko, title_en FROM products WHERE id=?', (product_id,)).fetchone()
    if not p:
        return
    text = (p['title_ko'] or '') + ' ' + (p['title_en'] or '')

    # 캡슐/정 수
    cap_m = PAT_CAP.search(text)
    capsule = int(cap_m.group(1)) if cap_m else 60  # default 60

    # 중량 (g) — mg 면 /1000
    weight_g: float = 0.5  # default
    weight_m = PAT_WEIGHT.search(text)
    if weight_m:
        v = float(weight_m.group(1))
        unit = weight_m.group(2).lower()
        weight_g = v / 1000.0 if unit == 'mg' else v

    # 용량 (ml) — oz → 29.57ml 환산
    volume_ml: float = 0.0
    vol_m = PAT_VOLUME.search(text)
    if vol_m:
        v = float(vol_m.group(1))
        unit = vol_m.group(2).lower()
        volume_ml = v * 29.5735 if unit == 'oz' else v

    # 수량 (1개 or 1세트)
    pack_m = PAT_PACK.search(text)
    quantity = 1
    if pack_m and 2 <= int(pack_m.group(1)) <= 10:
        quantity = 1  # 1세트

    # coupang_attributes_json schema: dict {속성명: "숫자 단위"}
    # 쿠팡은 0 값 invalid → 최소 양수.
    # 단위는 카테고리 meta 의 usableUnits 사용 (basicUnit '개' 가 usableUnits 에 없는 케이스 회피).
    weight_g = max(weight_g, 0.01)
    volume_ml = max(volume_ml, 0.01)
    attrs = {
        "개당 캡슐/정": f"{capsule} 정",      # usableUnits ['정', '회분']
        "개당 중량":   f"{weight_g:.2f} g",  # usableUnits ['g', 'kg']
        "개당 용량":   f"{volume_ml:.2f} ml", # usableUnits ['L', 'ml']
        "수량":       f"{quantity} 개",       # wing UI 가 '개' 사용 — '박스' 거부
    }
    with get_db() as conn:
        conn.execute(
            'UPDATE products SET coupang_attributes_json=? WHERE id=?',
            (json.dumps(attrs, ensure_ascii=False), product_id),
        )


async def reregister_pending(product_ids: list[int]) -> dict:
    """status='pending' 영양제 product 들을 list_product 재호출.

    coupang_lister.list_product 가 자동으로 build_payload + attributes 채움.
    옵션 부족 product 는 미리 fill_default_options 로 강제 채움.
    """
    if not product_ids:
        return {"ok": 0, "fail": 0, "skip": 0, "fail_details": []}

    # 모든 product 에 default 옵션 미리 채움 (정규식 추출 못 하면 default)
    for pid in product_ids:
        try:
            fill_default_options(pid)
        except Exception as e:
            logger.warning(f"[supp-rerun] fill_default fail pid={pid}: {e}")

    ok = 0
    fail = 0
    skip = 0
    fail_details = []

    sem = asyncio.Semaphore(2)  # 쿠팡 동시 등록 (분당 쿼터 안전한 수준)

    async def _register_one(pid: int):
        nonlocal ok, fail, skip
        async with sem:
            try:
                res = await asyncio.to_thread(list_product, pid)
            except Exception as e:
                fail += 1
                fail_details.append({"pid": pid, "err": f"예외: {str(e)[:120]}"})
                return
        if res.get("ok"):
            ok += 1
        elif res.get("skip"):
            skip += 1
        else:
            fail += 1
            fail_details.append({"pid": pid, "err": str(res.get("error", "?"))[:120]})

    await asyncio.gather(*[_register_one(pid) for pid in product_ids], return_exceptions=False)
    return {"ok": ok, "fail": fail, "skip": skip, "fail_details": fail_details}


async def run_full_rerun(
    blocklist_seller_pids: Optional[set[str]] = None,
    dry_run: bool = False,
) -> dict:
    """전체 흐름. dry_run=True 면 Phase 1-2 (식별 + 카테고리 UPDATE) 까지만.

    1. 영양제 listed 식별 (정책위반 제외)
    2. 카테고리 코드 UPDATE (모든 target)
    3. delete_product loop (한도 회전)
    4. status=pending reset
    5. list_product 재호출
    """
    targets = find_supplement_targets(blocklist_seller_pids)
    logger.info(f"[supp-rerun] 영양제 재등록 대상: {len(targets)}건")
    if not targets:
        return {"targets": 0}

    update_category_codes(targets)
    logger.info(f"[supp-rerun] coupang_category_code 일괄 UPDATE: {len(targets)}")

    if dry_run:
        return {"targets": len(targets), "dry_run": True}

    # stop_sales + reset
    delete_result = await stop_and_reset(targets)
    logger.info(f"[supp-rerun] stop: ok={delete_result['ok']} fail={delete_result['fail']}")

    # 재등록 — delete 성공한 것만 (status='pending' 인 것)
    pending_pids = []
    with get_db() as conn:
        for t in targets:
            r = conn.execute(
                "SELECT status FROM listings_pa WHERE product_id=? AND channel='coupang'",
                (t["product_id"],),
            ).fetchone()
            if r and r["status"] == "pending":
                pending_pids.append(t["product_id"])

    register_result = await reregister_pending(pending_pids)
    logger.info(
        f"[supp-rerun] register: ok={register_result['ok']} "
        f"fail={register_result['fail']} skip={register_result['skip']}"
    )

    return {
        "targets": len(targets),
        "delete": delete_result,
        "register": register_result,
    }
