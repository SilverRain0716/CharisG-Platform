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
from backend.purchase.services.safety_filter import is_banned_diet_product

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


async def _discover_siblings_for_promoted(sourcing_ids, job_id: str) -> dict:
    """promote 후처리 — 시트 ASIN 의 parent_asin 추출 + 같은 parent 의 형제 모두 INSERT.

    1) 방금 INSERT 된 product 의 parent_asin 모음 (sp_api_facts 가 채워둔 컬럼)
    2) parent 별 dedupe — 같은 그룹은 한 번만 처리
    3) discover_group(parent) → variation_groups upsert
    4) fetch_and_insert_children(parent) → 시트에 없는 형제 child 도 INSERT
    """
    from backend.purchase.services.sp_api_group_discovery import discover_group
    from backend.purchase.services.group_lister import fetch_and_insert_children

    sourcing_ids_list = list(sourcing_ids)
    if not sourcing_ids_list:
        return {"unique_parents": 0, "groups_created": 0, "extra_children": 0}

    with get_db() as conn:
        ph = ",".join("?" * len(sourcing_ids_list))
        rows = conn.execute(
            f"""SELECT DISTINCT parent_asin FROM products
                WHERE sourcing_id IN ({ph})
                  AND parent_asin IS NOT NULL AND parent_asin != ''""",
            sourcing_ids_list,
        ).fetchall()
    parents = [r["parent_asin"] for r in rows]
    if not parents:
        return {"unique_parents": 0, "groups_created": 0, "extra_children": 0}

    groups_created = 0
    extra_children = 0
    total = len(parents)

    for idx, parent in enumerate(parents, 1):
        try:
            await asyncio.to_thread(discover_group, parent)
            groups_created += 1
            res = await asyncio.to_thread(fetch_and_insert_children, parent)
            if isinstance(res, dict):
                extra_children += int(res.get("inserted") or 0)
        except Exception as e:
            logger.warning(f"[siblings] parent {parent} 실패: {e}")

        if idx % 5 == 0 or idx == total:
            try:
                with get_db() as conn:
                    conn.execute(
                        """UPDATE batch_jobs SET phase_message=? WHERE id=?""",
                        (f"형제 발견 {idx}/{total} (그룹 {groups_created}, 추가 {extra_children})", job_id),
                    )
            except Exception:
                pass

    return {
        "unique_parents": total,
        "groups_created": groups_created,
        "extra_children": extra_children,
    }


def _assign_categories_after_promote(sourcing_id_to_keyword: dict) -> dict:
    """promote 직후 호출 — 키워드 단위 카테고리 매핑.

    같은 키워드의 product 들은 첫 product 만 AI lookup, 나머지는 같은 결과 적용.
    needs_review 면 첫 product 의 review 큐만 INSERT.

    반환: {"keywords": N, "auto": 자동매핑된 product 수, "review": review 큐 INSERT 수, "skip": keyword 없음}
    """
    from backend.purchase.services.category_mapper import map_categories_for_keyword
    from backend.purchase.services.title_translator import ensure_title_ko

    stats = {"keywords": 0, "auto": 0, "review": 0, "skip": 0}

    # sourcing_id → product_id 매핑 (방금 INSERT 된 products)
    sourcing_ids = [sid for sid, kw in sourcing_id_to_keyword.items() if kw]
    if not sourcing_ids:
        stats["skip"] = len(sourcing_id_to_keyword)
        return stats

    with get_db() as conn:
        ph = ",".join("?" * len(sourcing_ids))
        rows = conn.execute(
            f"""SELECT id, sourcing_id, asin, title_en, title_ko, parent_asin
                FROM products WHERE sourcing_id IN ({ph})""",
            sourcing_ids,
        ).fetchall()

    keyword_to_products: dict[str, list] = {}
    for r in rows:
        kw = sourcing_id_to_keyword.get(r["sourcing_id"])
        if not kw:
            continue
        keyword_to_products.setdefault(kw, []).append(dict(r))

    stats["keywords"] = len(keyword_to_products)

    for kw, products in keyword_to_products.items():
        first = products[0]
        # title_ko 보강 (Fix 1-A) — 첫 product 만
        if first.get("asin"):
            try:
                ko = ensure_title_ko(first["asin"])
                if ko:
                    first["title_ko"] = ko
            except Exception as e:
                logger.warning(f"[promote-categories] ensure_title_ko 실패 {first.get('asin')}: {e}")

        product_name = first.get("title_ko") or first.get("title_en") or ""

        # E 강화: 같은 키워드의 다른 product 제목 샘플 (최대 3건) + 영문 원문 1건
        # → 키워드 모호성 해소 ("선글라스" → 사람용/반려동물용 구분, "칼날" → 면도기/공구 구분)
        sample_titles = []
        for p in products[:4]:  # first 포함 최대 4개 → 중복 제거 후 3개 확보
            t = p.get("title_ko") or p.get("title_en")
            if t and t not in sample_titles:
                sample_titles.append(t)
            if len(sample_titles) >= 3:
                break
        sample_en = first.get("title_en") or ""

        try:
            result = map_categories_for_keyword(
                keyword=kw,
                product_name=product_name,
                product_id=first["id"],
                product_name_en=first.get("title_en"),
                sample_titles=sample_titles,
                sample_en=sample_en,
            )
        except Exception as e:
            logger.warning(f"[promote-categories] map fail keyword={kw}: {e}")
            continue

        # 자동 매핑 (cache hit 또는 score>=50) → 같은 키워드 + 같은 parent 의 모든 product
        if not result.get("needs_review") and result.get("naver_id"):
            naver_id = result["naver_id"]
            updated_count = 0
            with get_db() as conn:
                # 1. 키워드의 모든 product
                for p in products:
                    conn.execute(
                        "UPDATE products SET category_path=? WHERE id=?",
                        (naver_id, p["id"]),
                    )
                    updated_count += 1
                # 2. 같은 parent 의 다른 형제 (시트에 없는 child 도) — 카테고리 비어있는 것만
                parent_asins = list({p["parent_asin"] for p in products if p.get("parent_asin")})
                for pa in parent_asins:
                    cur = conn.execute(
                        """UPDATE products SET category_path=?
                           WHERE parent_asin=?
                             AND (category_path IS NULL OR category_path='')""",
                        (naver_id, pa),
                    )
                    updated_count += cur.rowcount
            stats["auto"] += updated_count
        else:
            stats["review"] += 1  # review 큐는 첫 product 1건만 INSERT (map_categories_for_keyword 내부)

    return stats


def _enrich_from_sp_api(asin: str) -> dict:
    """SP-API로 상품정보 보강 — sp_api_facts (단일 호출 + 정규화 + DB 캐시) 경유.

    반환은 기존 호출자가 기대하던 키(title/brand/description/bullet_points/images)로
    매핑돼 있다. facts 자체는 sp_api_facts 가 products 테이블에 함께 저장한다
    (parent_asin / sp_api_facts_json / weight_g 등).
    """
    try:
        from backend.purchase.services.sp_api_facts import get_facts_for_promote
        return get_facts_for_promote(asin)
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
                """SELECT id, asin, title, price_usd, price_krw, image_url, category
                   FROM sourcing_candidates"""
            ).fetchall()
        finally:
            conn_read.close()
        # 시트의 "카테고리" 키워드 → 매핑 helper 입력 (Fix 1-D)
        sourcing_id_to_keyword = {
            r["id"]: ((r["category"] or "").strip().lower() or None) for r in rows
        }

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

        # ── 사전 dedup: 기존 products 와 ASIN 중복 체크 (옵션 2) ──
        # IntegrityError 로 전체 batch 가 fail 하던 문제 회피.
        # 신규 ASIN 만 SP-API 보강 + INSERT, 중복은 duplicates 리스트로 보고.
        candidate_asins = [r["asin"] for r in rows if r["asin"]]
        existing: dict[str, dict] = {}  # asin -> {id, status, sourcing_id}
        if candidate_asins:
            # IN 절 청크 (sqlite 변수 한도 999 회피)
            for i in range(0, len(candidate_asins), 500):
                chunk = candidate_asins[i:i + 500]
                with get_db() as conn:
                    rows_existing = conn.execute(
                        f"SELECT id, asin, status, sourcing_id FROM products "
                        f"WHERE asin IN ({','.join('?' * len(chunk))})",
                        chunk,
                    ).fetchall()
                for er in rows_existing:
                    existing[er["asin"]] = {
                        "id": er["id"], "status": er["status"], "sourcing_id": er["sourcing_id"],
                    }

        new_rows = [r for r in rows if r["asin"] and r["asin"] not in existing]
        duplicate_rows = [r for r in rows if r["asin"] and r["asin"] in existing]
        # 빈 ASIN 행도 신규로 처리 (희귀 케이스 — 이전 동작 유지)
        new_rows.extend(r for r in rows if not r["asin"])

        # duplicates 리포트 데이터
        duplicates_report = []
        for r in duplicate_rows:
            ex = existing[r["asin"]]
            duplicates_report.append({
                "asin": r["asin"],
                "sheet_keyword": (r["category"] or "").strip(),
                "sheet_title": r["title"],
                "existing_product_id": ex["id"],
                "existing_status": ex["status"],
            })

        new_total = len(new_rows)
        dup_total = len(duplicate_rows)
        logger.info(f"[promote-job {job_id}] dedup: 신규 {new_total}, 중복 skip {dup_total}")

        with get_db() as conn:
            conn.execute(
                "UPDATE batch_jobs SET total=?, phase_message=? WHERE id=?",
                (new_total,
                 f"신규 {new_total}건 SP-API 보강 시작 (중복 {dup_total}건 skip)" if dup_total
                 else f"SP-API 보강 0/{new_total}",
                 job_id),
            )

        # ── SP-API 보강 (신규 ASIN 만, 논블로킹 + rate limit) ──
        prepared = []
        enriched = 0
        errors = 0
        banned_diet: list[dict] = []  # 약사법 사전 차단 리스트

        for idx, r in enumerate(new_rows, start=1):
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
                if idx < new_total:
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

            # ── 약사법 1차 게이트: 식욕억제·GLP-1·다이어트 약 hard block ──
            matched_kw = is_banned_diet_product(title_en, "")
            if matched_kw:
                banned_diet.append({
                    "asin": asin or "",
                    "title": title_en[:120] if title_en else "",
                    "matched": matched_kw,
                })
                logger.warning(
                    f"[promote-job {job_id}] 약사법 차단 ({matched_kw}): "
                    f"asin={asin} title={title_en[:80]}"
                )
                continue

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
                    (idx, errors, f"SP-API 보강 {idx}/{new_total}", job_id),
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
            # OR IGNORE — race condition 방어 (사전 dedup 후에도 동시 promote 가능성)
            conn.executemany(
                """INSERT OR IGNORE INTO products
                   (sourcing_id, business_model, asin, title_en, description_en,
                    brand, cost_usd, images_json, status)
                   VALUES (?, 'purchase', ?, ?, ?, ?, ?, ?, 'draft')""",
                prepared,
            )
            # product_keywords (옵션 3) — sourcing_id 로 방금 INSERT 된 product_id 매핑 후 추가.
            # 같은 트랜잭션에서 처리 — 일관성 보장.
            sourcing_ids_inserted = [p[0] for p in prepared]
            if sourcing_ids_inserted:
                # sourcing_id → product_id 매핑 일괄 조회
                pid_map = {}
                for i in range(0, len(sourcing_ids_inserted), 500):
                    chunk = sourcing_ids_inserted[i:i + 500]
                    rows_pid = conn.execute(
                        f"SELECT id, sourcing_id FROM products "
                        f"WHERE sourcing_id IN ({','.join('?' * len(chunk))})",
                        chunk,
                    ).fetchall()
                    for pr in rows_pid:
                        pid_map[pr["sourcing_id"]] = pr["id"]

                pk_payload = []
                for sid in sourcing_ids_inserted:
                    pid = pid_map.get(sid)
                    kw = sourcing_id_to_keyword.get(sid)
                    if pid and kw:
                        pk_payload.append((pid, kw, sid))
                if pk_payload:
                    conn.executemany(
                        """INSERT OR IGNORE INTO product_keywords
                           (product_id, keyword, source_sourcing_id, is_primary)
                           VALUES (?, ?, ?, 1)""",
                        pk_payload,
                    )

            conn.execute("DELETE FROM sourcing_candidates")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        promoted = len(prepared)
        logger.info(
            f"[promote-job {job_id}] {promoted}건 이관, {enriched}건 SP-API 보강, "
            f"{errors}건 오류, 중복 skip {dup_total}건"
        )

        # ── 3단계 (B): parent_asin 별 형제 자동 발견 + variation_groups 생성 ──
        # 시트에 1 ASIN 만 있어도 SP-API 형제 발견 → 같은 parent 의 모든 child INSERT.
        # variation_groups 테이블에 파생 데이터 캐시 → 옵션 그룹 페이지에서 묶어 등록 가능.
        sib_stats = {"unique_parents": 0, "groups_created": 0, "extra_children": 0}
        try:
            with get_db() as conn:
                conn.execute(
                    "UPDATE batch_jobs SET phase_message=? WHERE id=?",
                    ("형제 ASIN 자동 발견 중", job_id),
                )
            sib_stats = await _discover_siblings_for_promoted(
                sourcing_id_to_keyword.keys(), job_id,
            )
            logger.info(f"[promote-job {job_id}] 형제 발견: {sib_stats}")
        except Exception as e:
            logger.warning(f"[promote-job {job_id}] 형제 자동 발견 실패 (계속 진행): {e}")

        # ── 4단계: 카테고리 매핑 (keyword 단위 batching, Fix 1-D) ──
        cat_stats = {"keywords": 0, "auto": 0, "review": 0, "skip": 0}
        try:
            with get_db() as conn:
                conn.execute(
                    "UPDATE batch_jobs SET phase_message=? WHERE id=?",
                    ("카테고리 매핑 중", job_id),
                )
            cat_stats = await asyncio.to_thread(
                _assign_categories_after_promote, sourcing_id_to_keyword,
            )
            logger.info(f"[promote-job {job_id}] 카테고리 매핑: {cat_stats}")
        except Exception as e:
            logger.warning(f"[promote-job {job_id}] 카테고리 매핑 실패 (계속 진행): {e}")

        # 결과 JSON (구조화 데이터 — UI 가 모달에 표시)
        result_data = {
            "new": promoted,
            "duplicate_skipped": dup_total,
            "enriched": enriched,
            "errors": errors,
            "duplicates": duplicates_report,
            "siblings": sib_stats,
            "categories": cat_stats,
            "banned_diet": banned_diet,
        }

        summary = (
            f"완료 — 신규 {promoted}건 / 중복 skip {dup_total}건 / "
            f"약사법 차단 {len(banned_diet)}건 / "
            f"SP-API 보강 {enriched}건 / "
            f"형제 그룹 {sib_stats.get('groups_created',0)}개 (+추가 {sib_stats.get('extra_children',0)}건) / "
            f"카테고리 자동 {cat_stats.get('auto',0)}건"
        )

        with get_db() as conn:
            conn.execute(
                """UPDATE batch_jobs
                   SET status='done', processed=?, errors=?, finished_at=?,
                       phase_message=?, result_json=?
                   WHERE id=?""",
                (
                    promoted, errors, _now_iso(), summary,
                    json.dumps(result_data, ensure_ascii=False),
                    job_id,
                ),
            )

        # Discord 알림 — 실패해도 본 작업은 성공
        try:
            from backend.purchase.services.notifier import notify_promote_complete
            notify_promote_complete(
                new_count=promoted, duplicate_count=dup_total,
                enriched=enriched, errors=errors,
                banned_diet=len(banned_diet),
            )
        except Exception as e:
            logger.warning(f"[promote-job {job_id}] Discord 알림 실패 (무시): {e}")

    except Exception as e:
        logger.exception(f"[promote-job {job_id}] 실패")
        with get_db() as conn:
            conn.execute(
                """UPDATE batch_jobs
                   SET status='error', error_message=?, finished_at=?
                   WHERE id=?""",
                (str(e), _now_iso(), job_id),
            )
