"""채널 업로드 사전 준비 — 카테고리 매핑 백그라운드 job.

두 종류의 매핑이 있고, 대상 테이블·시점이 다르므로 완전히 분리한다.

1) 네이버 카테고리 매핑 (job_type='naver_category_map')
   - 대상: products.category_path 가 영문 텍스트인 행 (네이버 숫자 leaf ID 아님)
   - 결과: products.category_path 를 네이버 leaf ID(숫자 문자열)로 갱신
   - 타이밍: '채널 보내기' 이전. 이후 listings_pa.category_mapped 로 복사됨
   - 로직: backend.purchase.scripts.bulk_prepare._find_best_category 재사용
     (키워드 후보 추출 → Gemini 최종 선택 → 실패 시 키워드 top-1 폴백)
   - 네이버 카테고리 풀은 naver_categories 테이블(4,993개)에서 로드

2) 쿠팡 카테고리 매핑 (job_type='coupang_category_map')
   - 대상: listings_pa 중 channel='coupang' AND coupang_category_code 비어있고
           category_mapped(네이버 ID) 가 있는 distinct naver_id 들
   - 결과: naver_coupang_category_map INSERT + listings_pa.coupang_category_code 갱신
   - 타이밍: '채널 보내기' 이후
   - 로직: backend.purchase.scripts.map_naver_to_coupang 의 stage1/2/3 재사용

공용:
   - batch_jobs 테이블 재사용 — job_type 으로 구분
   - get_running_job(job_type) 으로 동시 실행 방지
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timezone

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)

JOB_TYPE_NAVER = "naver_category_map"
JOB_TYPE_COUPANG = "coupang_category_map"

# AI 호출 간격 (Gemini rate limit 회피)
_AI_INTERVAL = 1.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 공용 batch_jobs 헬퍼 ─────────────────────────────────────

def create_job(job_type: str, total: int) -> str:
    job_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            """INSERT INTO batch_jobs (id, job_type, status, total, created_at, phase_message)
               VALUES (?, ?, 'pending', ?, ?, ?)""",
            (job_id, job_type, total, _now_iso(), "대기 중"),
        )
    return job_id


def get_job(job_id: str, job_type: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM batch_jobs WHERE id=? AND job_type=?",
            (job_id, job_type),
        ).fetchone()
    return dict(row) if row else None


def get_running_job(job_type: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM batch_jobs
               WHERE job_type=? AND status IN ('pending','running')
               ORDER BY created_at DESC LIMIT 1""",
            (job_type,),
        ).fetchone()
    return dict(row) if row else None


# ── 네이버 카테고리 매핑 ─────────────────────────────────────

def count_naver_pending() -> int:
    """category_path 가 네이버 숫자 ID 가 아닌 purchase 상품 수."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) c FROM products
               WHERE business_model='purchase'
                 AND category_path IS NOT NULL AND category_path != ''
                 AND ai_processed_at IS NOT NULL
                 AND CAST(category_path AS INTEGER) = 0"""
        ).fetchone()
    return row["c"] if row else 0


def _load_naver_cats() -> list[dict]:
    """naver_categories 테이블 → bulk_prepare 가 기대하는 dict 형식."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, whole_name FROM naver_categories WHERE is_leaf=1"
        ).fetchall()
    return [
        {"id": r["id"], "name": r["name"], "wholeCategoryName": r["whole_name"]}
        for r in rows
    ]


def _map_one_naver(text_path: str, title_ko: str, naver_cats: list[dict]) -> dict | None:
    """한 상품의 category_path 를 네이버 leaf ID 로 매핑. 실패 시 None."""
    from backend.purchase.scripts.bulk_prepare import _find_best_category
    return _find_best_category(text_path, title_ko or "", naver_cats)


async def run_naver_category_background(job_id: str) -> None:
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET status='running', started_at=?, phase_message=? WHERE id=?",
                (_now_iso(), "네이버 카테고리 목록 로드 중", job_id),
            )

        naver_cats = await asyncio.to_thread(_load_naver_cats)
        if not naver_cats:
            raise RuntimeError("naver_categories 테이블이 비어있음")

        with get_db() as conn:
            rows = conn.execute(
                """SELECT id, category_path, title_ko FROM products
                   WHERE business_model='purchase'
                     AND category_path IS NOT NULL AND category_path != ''
                     AND ai_processed_at IS NOT NULL
                     AND CAST(category_path AS INTEGER) = 0
                   ORDER BY id"""
            ).fetchall()

        total = len(rows)
        if total == 0:
            with get_db() as conn:
                conn.execute(
                    """UPDATE batch_jobs
                       SET status='done', total=0, finished_at=?, phase_message='매핑 대상 없음'
                       WHERE id=?""",
                    (_now_iso(), job_id),
                )
            return

        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET total=?, phase_message=? WHERE id=?",
                (total, f"매핑 중 0/{total}", job_id),
            )

        processed = 0
        errors = 0
        for idx, r in enumerate(rows, start=1):
            try:
                best = await asyncio.to_thread(
                    _map_one_naver, r["category_path"], r["title_ko"], naver_cats,
                )
                if best:
                    with get_db() as conn:
                        conn.execute(
                            "UPDATE products SET category_path=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (str(best["id"]), r["id"]),
                        )
                else:
                    errors += 1
                    logger.warning(f"[naver-map {job_id}] 매핑 실패 product {r['id']}: {r['category_path']}")
            except Exception as e:
                errors += 1
                logger.exception(f"[naver-map {job_id}] product {r['id']} 예외")
            processed += 1

            with get_db() as conn:
                conn.execute(
                    "UPDATE batch_jobs SET processed=?, errors=?, phase_message=? WHERE id=?",
                    (processed, errors, f"매핑 중 {processed}/{total}", job_id),
                )

            # Gemini rate limit 완화
            if idx < total:
                await asyncio.sleep(_AI_INTERVAL)

        succeeded = processed - errors
        with get_db() as conn:
            conn.execute(
                """UPDATE batch_jobs
                   SET status='done', finished_at=?, phase_message=?
                   WHERE id=?""",
                (_now_iso(), f"완료 — 성공 {succeeded}, 실패 {errors} / {total}", job_id),
            )
        logger.info(f"[naver-map {job_id}] 완료 — 성공 {succeeded}, 실패 {errors}")

    except Exception as e:
        logger.exception(f"[naver-map {job_id}] 실패")
        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET status='error', error_message=?, finished_at=? WHERE id=?",
                (str(e), _now_iso(), job_id),
            )


# ── 쿠팡 카테고리 매핑 ─────────────────────────────────────

def count_coupang_pending() -> int:
    """채널=쿠팡, coupang_category_code 가 비어있고 category_mapped(네이버ID) 존재하는
    listings_pa 건수 (distinct naver_id 기준이 아닌 listing 개수)."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) c FROM listings_pa
               WHERE channel='coupang'
                 AND category_mapped IS NOT NULL AND category_mapped != ''
                 AND (coupang_category_code IS NULL OR coupang_category_code='')"""
        ).fetchone()
    return row["c"] if row else 0


def _pending_naver_ids_for_coupang() -> list[str]:
    """쿠팡 매핑이 필요한 네이버 ID 목록 (아직 naver_coupang_category_map 에 없는 것)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT l.category_mapped AS naver_id
               FROM listings_pa l
               LEFT JOIN naver_coupang_category_map m
                 ON m.naver_id = l.category_mapped
               WHERE l.channel='coupang'
                 AND l.category_mapped IS NOT NULL AND l.category_mapped != ''
                 AND m.naver_id IS NULL"""
        ).fetchall()
    return [str(r["naver_id"]) for r in rows]


async def run_coupang_category_background(job_id: str) -> None:
    try:
        from backend.purchase.scripts.map_naver_to_coupang import (
            stage1_exact, stage2_path, stage3_ai, save_mapping, update_listings_pa,
        )

        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET status='running', started_at=?, phase_message=? WHERE id=?",
                (_now_iso(), "매핑 대상 수집 중", job_id),
            )

        pending = await asyncio.to_thread(_pending_naver_ids_for_coupang)
        total = len(pending)

        if total == 0:
            # 대상 없어도 listings_pa 일괄 업데이트는 시도 (혹시 최근에 map 추가된 게 있으면 반영)
            updated = await asyncio.to_thread(update_listings_pa, False)
            with get_db() as conn:
                conn.execute(
                    """UPDATE batch_jobs
                       SET status='done', total=0, finished_at=?, phase_message=?
                       WHERE id=?""",
                    (_now_iso(), f"매핑 대상 없음 (listings_pa {updated}건 갱신)", job_id),
                )
            return

        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET total=?, phase_message=? WHERE id=?",
                (total, f"매핑 중 0/{total}", job_id),
            )

        counters = {"exact": 0, "path": 0, "ai": 0, "fail": 0}

        for idx, naver_id in enumerate(pending, start=1):
            with get_db() as conn:
                row = conn.execute(
                    "SELECT name, whole_name FROM naver_categories WHERE id=?",
                    (naver_id,),
                ).fetchone()
            if not row:
                counters["fail"] += 1
            else:
                naver_name = row["name"]
                whole_name = row["whole_name"]
                try:
                    r1 = await asyncio.to_thread(stage1_exact, naver_id, naver_name)
                    if r1:
                        code, note = r1
                        await asyncio.to_thread(save_mapping, naver_id, code, "exact", note, False)
                        counters["exact"] += 1
                    else:
                        r2 = await asyncio.to_thread(stage2_path, naver_id, naver_name, whole_name)
                        if r2:
                            code, note = r2
                            await asyncio.to_thread(save_mapping, naver_id, code, "path", note, False)
                            counters["path"] += 1
                        else:
                            r3 = await asyncio.to_thread(stage3_ai, naver_id, naver_name, whole_name)
                            if r3:
                                code, note = r3
                                await asyncio.to_thread(save_mapping, naver_id, code, "ai", note, False)
                                counters["ai"] += 1
                                await asyncio.sleep(_AI_INTERVAL)  # AI rate limit
                            else:
                                counters["fail"] += 1
                except Exception as e:
                    counters["fail"] += 1
                    logger.exception(f"[coupang-map {job_id}] {naver_id} 예외")

            processed = idx
            errors = counters["fail"]
            ok = counters["exact"] + counters["path"] + counters["ai"]
            with get_db() as conn:
                conn.execute(
                    "UPDATE batch_jobs SET processed=?, errors=?, phase_message=? WHERE id=?",
                    (processed, errors,
                     f"매핑 중 {processed}/{total} (성공 {ok} · exact {counters['exact']} · path {counters['path']} · ai {counters['ai']})",
                     job_id),
                )

        # 매핑 테이블 → listings_pa 일괄 반영
        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET phase_message=? WHERE id=?",
                ("listings_pa 갱신 중", job_id),
            )
        updated = await asyncio.to_thread(update_listings_pa, False)

        ok = counters["exact"] + counters["path"] + counters["ai"]
        with get_db() as conn:
            conn.execute(
                """UPDATE batch_jobs
                   SET status='done', finished_at=?, phase_message=?
                   WHERE id=?""",
                (_now_iso(),
                 f"완료 — 성공 {ok} (exact {counters['exact']} · path {counters['path']} · ai {counters['ai']}) · 실패 {counters['fail']} · listings_pa {updated}건 갱신",
                 job_id),
            )
        logger.info(f"[coupang-map {job_id}] 완료 — {counters}, listings_pa {updated}")

    except Exception as e:
        logger.exception(f"[coupang-map {job_id}] 실패")
        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET status='error', error_message=?, finished_at=? WHERE id=?",
                (str(e), _now_iso(), job_id),
            )
