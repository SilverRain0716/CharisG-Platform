"""기존 products.category_path 한글 경로 → 숫자 leafCategoryId 일괄 재매핑.

RAG(resolve_category)로 후보 검색 + LLM 선택 → DB UPDATE.
병렬 처리 (AI_BATCH_CONCURRENCY 환경변수, 기본 8).
"""
import asyncio
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

from backend.purchase.database import get_db
from backend.purchase.services.category_rag import resolve_category


async def process_one(pid: int, title: str, hint: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        try:
            res = await resolve_category(title, source_hint=hint)
            mc = res.get("mapped_category") or ""
            if mc and mc.isdigit():
                with get_db() as conn:
                    conn.execute(
                        "UPDATE products SET category_path=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (mc, pid),
                    )
                return {"pid": pid, "ok": True, "mapped": mc, "method": res.get("method"), "name": res.get("category_name", "")}
            return {"pid": pid, "ok": False, "reason": "no_valid_id", "raw": mc}
        except Exception as e:
            return {"pid": pid, "ok": False, "reason": str(e)}


async def main():
    concurrency = int(os.environ.get("AI_BATCH_CONCURRENCY", "8"))
    sem = asyncio.Semaphore(concurrency)

    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, COALESCE(title_ko, title_en, '') AS title, COALESCE(category_path, '') AS cat
               FROM products
               WHERE (category_path IS NULL OR category_path = '' OR NOT (category_path GLOB '[0-9]*' AND LENGTH(category_path) BETWEEN 6 AND 12))
                 AND COALESCE(title_ko, title_en, '') != ''
               ORDER BY id"""
        ).fetchall()

    total = len(rows)
    logger.info(f"대상: {total}건 (숫자 ID가 아닌 category_path)")
    if not total:
        return

    tasks = [process_one(r["id"], r["title"], r["cat"], sem) for r in rows]
    done = 0
    ok = 0
    fail = 0
    methods: dict = {}

    for fut in asyncio.as_completed(tasks):
        res = await fut
        done += 1
        if res["ok"]:
            ok += 1
            methods[res.get("method", "?")] = methods.get(res.get("method", "?"), 0) + 1
        else:
            fail += 1
        if done % 25 == 0 or done == total:
            logger.info(f"진행 {done}/{total} — 성공 {ok}, 실패 {fail}")

    logger.info(f"완료 — 성공 {ok}, 실패 {fail} / 총 {total}")
    logger.info(f"방법 분포: {methods}")
    if fail > ok:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
