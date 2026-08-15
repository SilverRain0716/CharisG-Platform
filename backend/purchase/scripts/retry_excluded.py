"""excluded 상품들을 blacklist 우회 카테고리로 재매핑 후 pending 으로 복구.

- resolve_category(avoid_blacklist=True) 로 비규제 카테고리 후보 검색
- 새 카테고리가 blacklist 가 아니면 products.category_path 갱신 + listings_pa.status='pending'
- 전부 blacklist 면 유지 (삭제는 별도 UI 에서)
"""
import asyncio
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

from backend.purchase.database import get_db
from backend.purchase.services.category_rag import resolve_category, is_blacklisted_path


async def process_one(pid: int, title: str, hint: str, old_cat: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        try:
            res = await resolve_category(title, source_hint=hint, avoid_blacklist=True)
            new_cat = (res.get("mapped_category") or "").strip()
            new_name = res.get("category_name", "")
            if not new_cat or not new_cat.isdigit():
                return {"pid": pid, "ok": False, "reason": "no_new_id"}
            if is_blacklisted_path(new_name):
                return {"pid": pid, "ok": False, "reason": "still_blacklisted", "name": new_name}
            with get_db() as conn:
                conn.execute(
                    "UPDATE products SET category_path=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (new_cat, pid),
                )
                conn.execute(
                    """UPDATE listings_pa SET status='pending', error_message=NULL,
                       last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='smartstore' AND status='excluded'""",
                    (pid,),
                )
            return {
                "pid": pid, "ok": True,
                "old": old_cat, "new": new_cat, "name": new_name,
                "method": res.get("method"),
            }
        except Exception as e:
            return {"pid": pid, "ok": False, "reason": str(e)}


async def main():
    concurrency = int(os.environ.get("AI_BATCH_CONCURRENCY", "8"))
    sem = asyncio.Semaphore(concurrency)

    with get_db() as conn:
        rows = conn.execute(
            """SELECT p.id, COALESCE(p.title_ko, p.title_en, '') AS title,
                      COALESCE(p.category_path, '') AS old_cat
               FROM products p
               JOIN listings_pa l ON l.product_id = p.id
               WHERE l.channel='smartstore' AND l.status='excluded'
                 AND COALESCE(p.title_ko, p.title_en, '') != ''
               ORDER BY p.id"""
        ).fetchall()

    total = len(rows)
    logger.info(f"대상 excluded 상품: {total}건")
    if not total:
        return

    tasks = [process_one(r["id"], r["title"], "", r["old_cat"], sem) for r in rows]
    ok = fail = 0
    methods: dict = {}
    reasons: dict = {}

    for fut in asyncio.as_completed(tasks):
        res = await fut
        if res["ok"]:
            ok += 1
            methods[res.get("method", "?")] = methods.get(res.get("method", "?"), 0) + 1
            logger.info(f"  [OK {res['pid']}] {res['old']} → {res['new']} ({res['name']})")
        else:
            fail += 1
            reason = res.get("reason", "unknown")
            reasons[reason] = reasons.get(reason, 0) + 1
            if len([v for v in reasons.values() if v > 0]) <= 5:  # 초반 몇 건만 상세
                logger.info(f"  [FAIL {res['pid']}] reason={reason} name={res.get('name', '')}")

    logger.info(f"완료 — 성공 {ok} / 실패 {fail} / 총 {total}")
    logger.info(f"방법 분포: {methods}")
    logger.info(f"실패 사유 분포: {reasons}")
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
