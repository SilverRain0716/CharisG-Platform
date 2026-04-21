"""sourcing_candidates → products 일괄 이관 (SP-API 보강 포함).

워크플로우: 사용자가 Sourcing 페이지에서 "상품관리로 전체 이관" 버튼을 누르면
남아있는 모든 sourcing_candidates 행을 products 에 INSERT 한 뒤
sourcing_candidates 테이블을 비운다.

SP-API 보강: promote 시 각 ASIN에 대해 SP-API로 정확한 상품정보를 수집하여
title_en, description_en, brand, images_json 을 채운다.
시트에서 가져온 title이 인증 배지 설명 등으로 오염된 경우를 방지한다.

백그라운드 실행:
  SP-API rate limit(2 req/sec, 건당 0.55초 대기) 때문에 수백 건이면 수 분이 걸려
  Nginx proxy_read_timeout(120s)을 넘는다. 그래서 `batch_jobs` 테이블에 job을
  만들고 asyncio task 로 비동기 실행한다. 프론트는 job_id 로 폴링한다.

주의 — FK 제약 우회:
  products.sourcing_id 는 sourcing_candidates(id) 를 REFERENCES 하고
  database.get_db() 는 PRAGMA foreign_keys=ON 을 건다. 따라서 같은 트랜잭션에서
  INSERT 후 바로 부모 DELETE 하면 자식(방금 넣은 products 행)을 남긴 채 부모를
  지우려다 FOREIGN KEY constraint failed 로 깨진다.

  사용자 요구는 '이관 후 products.sourcing_id 를 이력 포인터로 남긴다' 이므로
  sourcing_id 를 NULL 로 지우는 건 설계 위반이다. 대신 이 오퍼레이션 전용
  커넥션을 열어 foreign_keys=OFF 로 두고 INSERT+DELETE 를 원자적으로 처리한다.
  DELETE 후 products.sourcing_id 는 부모가 사라진 dangling 포인터가 되지만,
  products.asin + products.created_at 으로 충분히 추적 가능하다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

from backend.purchase.database import DB_PATH, get_db
from backend.purchase.services.exchange_rate_service import get_current_rate

logger = logging.getLogger(__name__)

JOB_TYPE = "sourcing_promote"

# SP-API rate limit: 2 req/sec
_SP_API_INTERVAL = 0.55


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── batch_jobs 헬퍼 (job_type='sourcing_promote' 전용) ──

def create_promote_job(total: int) -> str:
    job_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            """INSERT INTO batch_jobs (id, job_type, status, total, created_at, phase_message)
               VALUES (?, ?, 'pending', ?, ?, ?)""",
            (job_id, JOB_TYPE, total, _now_iso(), "대기 중"),
        )
    return job_id


def get_promote_job(job_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM batch_jobs WHERE id=? AND job_type=?",
            (job_id, JOB_TYPE),
        ).fetchone()
    return dict(row) if row else None


def get_running_promote_job() -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM batch_jobs
               WHERE job_type=? AND status IN ('pending','running')
               ORDER BY created_at DESC LIMIT 1""",
            (JOB_TYPE,),
        ).fetchone()
    return dict(row) if row else None


def _enrich_from_sp_api(asin: str) -> dict:
    """SP-API로 상품정보 보강. 실패 시 빈 dict 반환. (동기 — to_thread 래핑용)"""
    try:
        from backend.purchase.services.image_downloader import fetch_product_info_sp_api
        return fetch_product_info_sp_api(asin)
    except Exception as e:
        logger.warning(f"SP-API 보강 실패 ({asin}): {e}")
        return {}


async def run_promote_background(job_id: str) -> None:
    """백그라운드 asyncio task 진입점. batch_jobs 레코드를 갱신하며 진행한다.

    처리 단계:
      1. candidates 읽기
      2. 각 ASIN 마다 SP-API 보강 (to_thread 로 논블로킹, rate limit 0.55초)
      3. INSERT + DELETE (짧은 트랜잭션)
    """
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET status='running', started_at=?, phase_message=? WHERE id=?",
                (_now_iso(), "후보 목록 읽는 중", job_id),
            )

        # ── 1단계: 후보 읽기 ──
        conn_read = sqlite3.connect(str(DB_PATH))
        conn_read.row_factory = sqlite3.Row
        try:
            rows = conn_read.execute(
                "SELECT id, asin, title, price_usd, price_krw, image_url FROM sourcing_candidates"
            ).fetchall()
        finally:
            conn_read.close()

        total = len(rows)
        if total == 0:
            with get_db() as conn:
                conn.execute(
                    """UPDATE batch_jobs
                       SET status='done', total=0, processed=0, errors=0,
                           finished_at=?, phase_message='이관할 후보 없음'
                       WHERE id=?""",
                    (_now_iso(), job_id),
                )
            return

        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET total=?, phase_message=? WHERE id=?",
                (total, f"SP-API 보강 0/{total}", job_id),
            )

        # ── SP-API 보강 (논블로킹 + rate limit) ──
        prepared = []
        enriched = 0
        errors = 0

        for idx, r in enumerate(rows, start=1):
            asin = r["asin"]
            sheet_title = r["title"]
            sheet_image = r["image_url"]
            cost_usd = r["price_usd"]
            if cost_usd is None and r["price_krw"] is not None:
                # KRW-only 후보 — 환율로 USD 대체값 산출 (downstream 은 USD 계약 유지)
                cost_usd = r["price_krw"] / get_current_rate()

            if asin:
                try:
                    sp = await asyncio.to_thread(_enrich_from_sp_api, asin)
                except Exception as e:
                    logger.warning(f"[promote-job {job_id}] SP-API 예외 ({asin}): {e}")
                    sp = {}
                    errors += 1
                # rate limit — 마지막 항목 뒤에는 대기 불필요
                if idx < total:
                    await asyncio.sleep(_SP_API_INTERVAL)
            else:
                sp = {}

            title_en = sp.get("title") or sheet_title

            description_en = sp.get("description") or ""
            bullet_points = sp.get("bullet_points")
            if not description_en and bullet_points:
                description_en = "\n".join(f"• {bp}" for bp in bullet_points)

            brand = sp.get("brand") or ""

            sp_images = sp.get("images", [])
            if sp_images:
                images_json = json.dumps(sp_images, ensure_ascii=False)
            elif sheet_image:
                images_json = json.dumps([sheet_image], ensure_ascii=False)
            else:
                images_json = None

            prepared.append((
                r["id"], asin, title_en, description_en, brand,
                cost_usd, images_json,
            ))
            if sp:
                enriched += 1

            # 진행률 갱신 (매 건마다 — 496건이면 약 5분 걸림, 1건당 UPDATE 부담 없음)
            with get_db() as conn:
                conn.execute(
                    """UPDATE batch_jobs
                       SET processed=?, errors=?, phase_message=?
                       WHERE id=?""",
                    (idx, errors, f"SP-API 보강 {idx}/{total}", job_id),
                )

        # ── 2단계: 일괄 INSERT + DELETE ──
        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET phase_message=? WHERE id=?",
                ("products 테이블에 INSERT 중", job_id),
            )

        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.executemany(
                """INSERT INTO products
                   (sourcing_id, business_model, asin, title_en, description_en,
                    brand, cost_usd, images_json, status)
                   VALUES (?, 'purchase', ?, ?, ?, ?, ?, ?, 'draft')""",
                prepared,
            )
            conn.execute("DELETE FROM sourcing_candidates")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        promoted = len(prepared)
        logger.info(f"[promote-job {job_id}] {promoted}건 이관, {enriched}건 SP-API 보강, {errors}건 오류")

        with get_db() as conn:
            conn.execute(
                """UPDATE batch_jobs
                   SET status='done', processed=?, errors=?, finished_at=?,
                       phase_message=?
                   WHERE id=?""",
                (
                    promoted,
                    errors,
                    _now_iso(),
                    f"완료 — {promoted}건 이관, {enriched}건 SP-API 보강",
                    job_id,
                ),
            )

    except Exception as e:
        logger.exception(f"[promote-job {job_id}] 실패")
        with get_db() as conn:
            conn.execute(
                """UPDATE batch_jobs
                   SET status='error', error_message=?, finished_at=?
                   WHERE id=?""",
                (str(e), _now_iso(), job_id),
            )
