"""category_path 일별 backfill — 매일 KST 04:00 (UTC 19:00) 1000건씩.

흐름 (Option β: cache + AI fallback):
  1. batch_jobs INSERT (job_type='category_backfill')
  2. SELECT 1000 products WHERE category_path 빈 값 ORDER BY RANDOM()
  3. 각 product:
     a. product_keywords 의 primary keyword 추출 (없으면 any keyword)
     b. map_categories_for_keyword(keyword, title_ko, pid, ...) 호출
        → cache hit / miss(AI) / fail → 결과 dict 반환
     c. needs_review=False AND naver_id 존재 → UPDATE products.category_path + listings_pa
     d. needs_review=True → map_categories 내부에서 이미 category_review_queue INSERT
  4. batch_jobs UPDATE (done + result_json)

random sample 이유: cache miss product 가 다음날 cache 자라면 재시도되어 매핑될 수 있음.

서비스: systemd timer charisg-category-backfill (sudo enable 필요).
"""
import sys
import uuid
sys.path.insert(0, "/home/ubuntu/CharisG-Platform/charisg-platform")

from dotenv import load_dotenv
load_dotenv("/home/ubuntu/CharisG-Platform/charisg-platform/.env")

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone

from backend.purchase.services.category_mapper import map_categories_for_keyword

# ★2026-06-02 버그수정: standalone 스크립트라 db_factory 미등록 → category_mapper 내부
# translate_text(backend_shared.context.get_db) 가 "db_factory 미등록" 으로 실패하며
# 한글 AI 카테고리 분류가 degraded 됐음. get_db 등록으로 해소 (busy_timeout 도 get_db 경유).
from backend.purchase.database import get_db as _pa_get_db
from backend_shared.context import register_db_factory
register_db_factory(_pa_get_db)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("category-backfill-daily")

DB = "/home/ubuntu/CharisG-Platform/charisg-platform/backend/purchase/purchase.db"
DAILY_LIMIT = 1000


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    # 2026-05-16 guard: sheet pipeline 진행 중이면 즉시 skip. D 가 sheet 의 ai_detail 과
    # DB lock + Gemini quota 경합으로 양쪽 다 5x 느려지는 사고 회피 (2026-05-16 첫 firing 사고).
    guard_conn = sqlite3.connect(DB, timeout=10)
    in_progress = guard_conn.execute(
        "SELECT id, sheet_label FROM sheet_queue "
        "WHERE status NOT IN ('queued','done','error','cancelled')"
    ).fetchall()
    guard_conn.close()
    if in_progress:
        logger.warning(
            f"sheet pipeline 진행 중 → D backfill skip. "
            f"in-progress sids: {[(r[0], r[1]) for r in in_progress]}"
        )
        return

    job_id = uuid.uuid4().hex[:12]
    started = _now()
    conn = sqlite3.connect(DB, timeout=120)
    conn.execute("PRAGMA busy_timeout=180000")
    conn.row_factory = sqlite3.Row

    # 1. batch_jobs INSERT
    conn.execute(
        """INSERT INTO batch_jobs (id, job_type, status, total, created_at, started_at)
           VALUES (?, 'category_backfill', 'running', 0, ?, ?)""",
        (job_id, started, started),
    )
    conn.commit()
    logger.info(f"=== category_backfill {job_id} 시작 ===")

    # 2. 후보 추출
    rows = conn.execute(
        """SELECT p.id pid, p.title_ko, p.title_en,
                  (SELECT keyword FROM product_keywords
                    WHERE product_id=p.id ORDER BY is_primary DESC, id ASC LIMIT 1) kw
             FROM products p
            WHERE p.category_path IS NULL OR p.category_path=''
            ORDER BY RANDOM()
            LIMIT ?""",
        (DAILY_LIMIT,),
    ).fetchall()

    total = len(rows)
    conn.execute(
        "UPDATE batch_jobs SET total=? WHERE id=?",
        (total, job_id),
    )
    conn.commit()
    logger.info(f"  후보 {total}건 추출")

    if total == 0:
        logger.info("  더 이상 backfill 대상 없음 — 완료")
        conn.execute(
            """UPDATE batch_jobs SET status='done', processed=0, errors=0,
                                     finished_at=?, result_json=?
               WHERE id=?""",
            (_now(), json.dumps({"no_targets": True}), job_id),
        )
        conn.commit()
        conn.close()
        return

    # 3. 각 product 매핑
    hits = 0           # cache hit (즉시 매핑)
    ai_hits = 0        # AI 호출로 신규 매핑
    review_pushed = 0  # score 낮아서 review queue 로
    no_keyword = 0     # keyword 없음 → skip
    errors = 0
    processed = 0

    for i, r in enumerate(rows, 1):
        pid = r["pid"]
        kw = r["kw"]
        title_ko = r["title_ko"] or ""
        title_en = r["title_en"] or ""

        if not kw:
            no_keyword += 1
            processed += 1
            continue

        try:
            result = map_categories_for_keyword(
                keyword=kw,
                product_name=title_ko or title_en,
                product_id=pid,
                product_name_en=title_en or None,
            )
            from_cache = bool(result.get("from_cache"))
            needs_review = bool(result.get("needs_review"))
            naver_id = result.get("naver_id")
            coupang_code = result.get("coupang_code")

            if naver_id and not needs_review:
                # products + listings_pa 양쪽 UPDATE
                conn.execute(
                    "UPDATE products SET category_path=? "
                    "WHERE id=? AND (category_path IS NULL OR category_path='')",
                    (str(naver_id), pid),
                )
                conn.execute(
                    "UPDATE listings_pa SET category_mapped=? "
                    "WHERE product_id=? AND (category_mapped IS NULL OR category_mapped='')",
                    (str(naver_id), pid),
                )
                if coupang_code:
                    conn.execute(
                        "UPDATE listings_pa SET coupang_category_code=? "
                        "WHERE product_id=? AND channel='coupang' AND coupang_category_code IS NULL",
                        (int(coupang_code), pid),
                    )
                if from_cache:
                    hits += 1
                else:
                    ai_hits += 1
            else:
                review_pushed += 1
        except Exception as e:
            errors += 1
            logger.warning(f"  pid={pid} kw='{kw}' 실패: {str(e)[:120]}")

        processed += 1

        if i % 50 == 0:
            conn.execute(
                "UPDATE batch_jobs SET processed=?, errors=?, current_product_id=? WHERE id=?",
                (processed, errors, pid, job_id),
            )
            conn.commit()
            logger.info(
                f"  progress {i}/{total}  hit={hits} ai={ai_hits} "
                f"review={review_pushed} no_kw={no_keyword} err={errors}"
            )

    # 4. 마무리
    result_json = json.dumps({
        "cache_hits": hits,
        "ai_hits": ai_hits,
        "review_pushed": review_pushed,
        "no_keyword": no_keyword,
        "errors": errors,
        "total": total,
    })
    conn.execute(
        """UPDATE batch_jobs SET status='done', processed=?, errors=?,
                                 finished_at=?, result_json=?
           WHERE id=?""",
        (processed, errors, _now(), result_json, job_id),
    )
    conn.commit()
    conn.close()
    logger.info(
        f"=== {job_id} 완료 ===\n"
        f"  cache_hits={hits}, ai_hits={ai_hits}, "
        f"review_pushed={review_pushed}, no_keyword={no_keyword}, errors={errors}"
    )


if __name__ == "__main__":
    main()
