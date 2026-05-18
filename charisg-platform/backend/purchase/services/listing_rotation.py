"""Smartstore listings rotation — 10,000 한도 회전 처리.

신규 등록 시 한도 초과 예상이면 가장 오래된 무매출 상품 N개를 delete_product 처리하여
한도 여유 확보 후 신규 등록.

⚠️ 영구 삭제 — 네이버 한도 카운트는 SALE+SUSPENSION+WAIT+... 모든 상태 합산이라
    stop_sales 로는 한도가 안 빠짐. 회전이 의미가 있으려면 delete 필수.
    DB 의 products 정보는 보존되므로 새 listings_pa 행으로 다시 등록은 가능.

매출 매칭 범위:
- 현재: hot.orders (purchase_hot.db)
- 옛 매출: backup DB (C1 마이그 직전 snapshot)

상태 마킹:
- 성공 : status='rotated' (UI 통계 별도)
- 실패 : status 그대로 + error_message 기록
"""
import asyncio
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

from backend.purchase.database import DB_PATH, DB_PATH_HOT, get_db
from backend.purchase.services.naver_commerce_service import delete_product, get_sale_product_count

logger = logging.getLogger(__name__)

SMARTSTORE_LIMIT = 10000
SAFE_BUFFER = 50  # 한도 - SAFE_BUFFER 이하 유지

# C1 마이그 직전 backup — 옛 매출 보호용
BACKUP_DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db.bak.before_c1.20260428_004801"


def _backup_exists() -> bool:
    return Path(BACKUP_DB).exists()


def get_listed_count_db() -> int:
    """DB 내부 listings_pa.status='listed' 건수 (참고용)."""
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM listings_pa WHERE channel='smartstore' AND status='listed'"
        ).fetchone()[0]


def get_listed_count() -> int:
    """네이버 측 실제 SALE 상태 상품 수. 한도 측정의 기준값.

    네이버 API 호출 실패 시 DB 카운트로 fallback (실패해서 swap 안 하는 것보단 진행).
    """
    naver_count = get_sale_product_count()
    if naver_count is not None:
        return naver_count
    logger.warning("[rotation] 네이버 SALE 카운트 조회 실패 — DB fallback")
    return get_listed_count_db()


def find_swap_candidates(n: int) -> list[dict]:
    """매출 0 + 가장 오래된 listed smartstore N개 반환.

    매출 매칭: hot.orders + backup DB orders 양쪽.
    반환: [{"product_id":..., "channel_product_id":..., "created_at":..., "title_ko":...}, ...]
    """
    if n <= 0:
        return []

    # backup DB 의 매출 발생 product_id 조회 (smartstore 만)
    backup_pids: set[int] = set()
    if _backup_exists():
        try:
            b = sqlite3.connect(BACKUP_DB)
            b.row_factory = sqlite3.Row
            backup_pids = {
                r["product_id"]
                for r in b.execute(
                    "SELECT DISTINCT product_id FROM orders "
                    "WHERE channel='smartstore' AND product_id IS NOT NULL"
                ).fetchall()
            }
            b.close()
        except Exception as e:
            logger.warning(f"[rotation] backup DB 조회 실패 (무시하고 진행): {e}")

    # hot.db attach 후 후보 식별
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"ATTACH DATABASE '{DB_PATH_HOT}' AS hot")
        rows = conn.execute(
            """SELECT l.product_id, l.channel_product_id, l.created_at, p.title_ko
               FROM listings_pa l
               JOIN products p ON p.id = l.product_id
               WHERE l.channel='smartstore'
                 AND l.status='listed'
                 AND l.channel_product_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM hot.orders o
                     WHERE o.product_id = l.product_id
                 )
               ORDER BY l.created_at ASC
               LIMIT ?""",
            (n + len(backup_pids),),  # backup 매출 product 만큼 여유
        ).fetchall()
    finally:
        conn.close()

    # backup DB 매출 product 제외
    out = []
    for r in rows:
        if r["product_id"] in backup_pids:
            continue
        out.append(dict(r))
        if len(out) >= n:
            break
    return out


async def swap_oldest_no_sales(n: int, job_id: Optional[str] = None) -> dict:
    """가장 오래된 무매출 listed smartstore N개 delete_product 처리.

    ⚠️ 영구 삭제 — 네이버 한도가 모든 상태 합산이라 stop_sales 로는 회전 안 됨.
    DB 의 products 정보는 보존됨 (새 listings_pa 행으로 재등록 가능).

    환경변수 PA_DISABLE_AUTO_ROTATION=1 이면 호출 즉시 no-op 반환 (운영자 수동
    전환 시 무매출 상품 자동 영구 삭제 차단).

    각 건에 대해:
      1. naver_commerce_service.delete_product(originProductNo)
      2. listings_pa.status='rotated' (성공) 또는 error_message (실패)

    Discord 알림은 caller 가 결과 받아서 별도 호출.
    """
    if os.environ.get("PA_DISABLE_AUTO_ROTATION") == "1":
        logger.warning(
            "[rotation] PA_DISABLE_AUTO_ROTATION=1 — swap %d건 요청 차단됨 "
            "(자동 영구 삭제 비활성화)", n,
        )
        return {
            "requested": n, "candidates": 0, "ok": 0, "fail": 0, "details": [],
            "disabled": True,
        }

    candidates = find_swap_candidates(n)
    if not candidates:
        return {"requested": n, "candidates": 0, "ok": 0, "fail": 0, "details": []}

    ok = 0
    fail = 0
    details = []

    # 동시성 1 — 네이버 API rate limit (sem=1 직렬, 서비스의 다른 호출과 충돌 회피)
    sem = asyncio.Semaphore(1)

    async def _swap_one(c: dict):
        nonlocal ok, fail
        cpid = c["channel_product_id"]
        pid = c["product_id"]
        async with sem:
            success, err = await asyncio.to_thread(delete_product, str(cpid))
        if success:
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='rotated',
                       error_message=NULL,
                       last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='smartstore'""",
                    (pid,),
                )
            ok += 1
            details.append({"pid": pid, "cpid": cpid, "ok": True})
        else:
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET error_message=?,
                       last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='smartstore'""",
                    (f"rotation 실패: {err[:200]}", pid),
                )
            fail += 1
            details.append({"pid": pid, "cpid": cpid, "ok": False, "err": err[:120]})
            logger.warning(f"[rotation] pid={pid} cpid={cpid} 실패: {err[:120]}")

    # rate limit 안전 — 카테고리/등록과 다른 sem 이라 동시 가능하지만, 신중을 기해 직렬
    for c in candidates:
        await _swap_one(c)
        await asyncio.sleep(0.5)  # 네이버 API 분당 쿼터 회피

    logger.info(f"[rotation] swap 완료 — 요청 {n} / 후보 {len(candidates)} / 성공 {ok} / 실패 {fail}")
    return {
        "requested": n,
        "candidates": len(candidates),
        "ok": ok,
        "fail": fail,
        "details": details,
    }


def calculate_swap_needed(new_register_count: int) -> int:
    """신규 N건 등록할 때 swap 이 몇 건 필요한지 계산.

    listed_count + new_count > LIMIT - BUFFER 면 차이만큼 swap.
    """
    listed_count = get_listed_count()
    target = SMARTSTORE_LIMIT - SAFE_BUFFER
    needed = (listed_count + new_register_count) - target
    return max(0, needed)
