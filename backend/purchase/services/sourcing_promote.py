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
from backend.purchase.services import clean_policy, safety_filter, manufacturer_classifier
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


def _enrich_from_sp_api(asin: str, retries: int = 2) -> dict:
    """SP-API로 상품정보 보강. 실패 시 빈 dict 반환. (동기 — to_thread 래핑용)

    2026-05-21: retry 추가. 503/429/Throttle 시 3초 backoff × 2회.
    title 비어 있으면 1회 추가 retry (transient empty response 회피).
    """
    import time as _time
    from backend.purchase.services.image_downloader import fetch_product_info_sp_api

    last_err = ""
    for attempt in range(retries + 1):
        try:
            res = fetch_product_info_sp_api(asin)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            # transient 만 retry (5xx/throttle), 그 외는 즉시 중단
            msg = last_err.lower()
            if any(k in msg for k in ("503", "throttl", "timeout", "429", "5xx")):
                if attempt < retries:
                    _time.sleep(3)
                    continue
            logger.warning(f"SP-API 보강 실패 ({asin}): {last_err}")
            return {}
        # 응답 OK — title 있으면 즉시 return, 없으면 transient empty 가능성 — 1회 retry
        if res.get("title"):
            return res
        if attempt < retries:
            _time.sleep(3)
            continue
        return res  # 마지막 시도 결과 (title 없을 수도)
    return {}


async def run_promote_background(job_id: str, yield_unit=None) -> None:
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
                           finished_at=?, phase_message='이관할 후보 없음',
                           result_json=?
                       WHERE id=?""",
                    (
                        _now_iso(),
                        json.dumps({"new": 0, "enriched": 0, "blocked": 0,
                                    "errors": 0, "duplicate_skipped": 0}),
                        job_id,
                    ),
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
        korean_mfr_cache: dict[str, bool] = {}  # batch 내 brand 분류 캐시 (Naver+Gemini 호출 절감)
        blocked_violations = 0  # 클린 정책 차단 카운터
        errors = 0
        # 2026-06-02: 기존 products ASIN 대조 셋 — 중복 product 생성 방지.
        # 근원: 재임포트/재디스커버리 시 같은 ASIN 이 다시 promote 돼 중복 product 양산
        # (products 에 UNIQUE(asin) 없음 + promote 가 기존 대조 안 했음). 여기서 차단.
        _conn_dup = sqlite3.connect(str(DB_PATH))
        try:
            existing_asins = set(
                x[0] for x in _conn_dup.execute(
                    "SELECT asin FROM products WHERE asin IS NOT NULL AND asin != ''"
                ).fetchall()
            )
        finally:
            _conn_dup.close()
        duplicate_skipped = 0
        seen_asins = set()  # 2026-06-09 배치 내 동일 ASIN 중복 차단(스냅샷 누수 보완)

        # 2026-05-19: 환율을 루프 진입 시 1번만 — 행마다 DB query 부담 회피
        cached_fx_rate = get_current_rate()

        for idx, r in enumerate(rows, start=1):
            # 긴 promote 루프 — 25건마다 배치잡(writer) 대기 시 락 양보(타임잡 굶김 방지)
            if yield_unit is not None and idx % 25 == 1:
                await yield_unit.checkpoint()
            asin = r["asin"]
            # 중복 차단 — 이미 products 에 있는 ASIN 은 새 product 생성 안 함
            # (candidate 는 루프 후 line 'DELETE FROM sourcing_candidates' 로 일괄 정리됨)
            if asin and (asin in existing_asins or asin in seen_asins):
                duplicate_skipped += 1
                continue
            if asin:
                seen_asins.add(asin)
            sheet_title = r["title"]
            sheet_image = r["image_url"]
            cost_usd = r["price_usd"]
            if cost_usd is None and r["price_krw"] is not None:
                # KRW-only 후보 — 환율로 USD 대체값 산출 (downstream 은 USD 계약 유지)
                cost_usd = r["price_krw"] / cached_fx_rate

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

            # ★2026-08-11: sheet_title 폴백은 SP-API 제목이 비었을 때만 쓴다.
            #   종전에도 or 폴백이었지만, 시트 값이 다른 상품 제목인 사례가 실측됐다
            #   (커피 원두용기 → '아기 담요'). 어느 출처를 썼는지 로그로 남긴다.
            _sp_title = (sp.get("title") or "").strip()
            _sheet_title = (sheet_title or "").strip()
            title_en = _sp_title or _sheet_title
            if not _sp_title and _sheet_title:
                logger.warning(
                    f"[promote-job {job_id}] SP-API 제목 없음 → 시트 제목 사용: "
                    f"ASIN={asin} title={_sheet_title[:60]!r} "
                    f"(★시트 값이 다른 상품일 수 있음 — 검수 대상)"
                )

            # 2026-05-21: SP-API + sheet title 모두 비면 product 생성 skip.
            # Why: title 없는 product 는 AI 번역/리스팅 단계에서 무조건 실패 → drift 만 누적.
            #       promote 단계에서 차단해 sourcing_candidates 도 함께 정리됨 (DELETE).
            if not title_en:
                clean_policy.log_violation(
                    stage='sourcing',
                    violation_type='title_missing',
                    action_taken='blocked',
                    matched_keyword=asin or '(no asin)',
                    asin=asin,
                    original_text='',
                    notes=f'sourcing_id={r["id"]} SP-API title + sheet title 모두 빈 값 — promote skip',
                )
                logger.warning(
                    f'[promote-job {job_id}] title 부재 차단: ASIN={asin} sourcing_id={r["id"]}'
                )
                blocked_violations += 1
                continue

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

            # SP-API 판매가 우선 적용 (단 양수일 때만 — list_price 가 0/MSRP 미설정 케이스 다수)
            # 2026-05-19 사고: SP-API list_price=0/null 인 ASIN 의 시트 가격까지 0 으로 덮어
            # cost_usd=0 product 2,299건 생성 → 마진 0 listing 1,141 발생. amazon_price_usd>0 가드 추가.
            amazon_price_usd = sp.get("amazon_price_usd")
            if amazon_price_usd is not None and amazon_price_usd > 0:
                cost_usd = amazon_price_usd
            else:
                amazon_price_usd = None  # 0 은 DB 에 0 저장하지 말고 NULL

            # 크기/무게
            dimensions = sp.get("dimensions")
            dimensions_json = json.dumps(dimensions, ensure_ascii=False) if dimensions else None
            # dimensions에서 weight_g 추출 (weight_g 컬럼용)
            weight_g = None
            if dimensions and dimensions.get("weight") is not None:
                try:
                    w_val = float(dimensions["weight"])
                    w_unit = (dimensions.get("weight_unit") or "").lower()
                    if "pound" in w_unit or w_unit == "lb":
                        weight_g = int(w_val * 453.592)
                    elif "ounce" in w_unit or w_unit == "oz":
                        weight_g = int(w_val * 28.3495)
                    elif "kilogram" in w_unit or w_unit == "kg":
                        weight_g = int(w_val * 1000)
                    elif "gram" in w_unit or w_unit == "g":
                        weight_g = int(w_val)
                    else:
                        # 2026-05-19: 단위 불명 시 pounds 가정 금지 — 50g 짜리를 22.7kg 으로 잘못
                        # 변환해 배송비 폭증 + 마진 음수 위험. weight_g=None 유지.
                        # forwarder_shipping_usd 가 None 일 때 보수적 LBS fallback 사용.
                        logger.warning(
                            f'[promote-job {job_id}] weight_unit 불명 ({w_unit!r}) — '
                            f'weight_g 미설정 ASIN={asin} w_val={w_val}'
                        )
                        weight_g = None

                    # 2026-05-19 sanity: 50kg 초과는 SP-API/카탈로그 오류 의심 (DB 검증: B0FB3NB43R
                    # 등 1.4톤~1.99톤 weight 반환 케이스 확인됨). 보수적으로 weight_g=None 처리해
                    # forwarder_shipping_usd 가 LBS fallback 사용.
                    if weight_g is not None and weight_g > 50_000:
                        logger.warning(
                            f'[promote-job {job_id}] weight_g 비정상 (>50kg) — null 처리 '
                            f'ASIN={asin} w_val={w_val} unit={w_unit!r} weight_g={weight_g}'
                        )
                        weight_g = None
                except (ValueError, TypeError):
                    pass

            # 바코드 (EAN/UPC/GTIN)
            identifiers = sp.get("identifiers")
            identifiers_json = json.dumps(identifiers, ensure_ascii=False) if identifiers else None

            # 아마존 카테고리
            classifications = sp.get("classifications")
            amazon_category_json = json.dumps(classifications, ensure_ascii=False) if classifications else None

            # ── 도서 — 오디오북/ebook 차단 (구매대행 부적합) ──
            product_type = sp.get('product_type') if sp else None
            book_blocked, book_kw = clean_policy.check_prohibited_book(
                title_en or '', product_type or '',
            )
            if book_blocked:
                clean_policy.log_violation(
                    stage='sourcing', violation_type='prohibited_book',
                    action_taken='blocked', matched_keyword=book_kw,
                    asin=asin, original_text=title_en,
                    notes=f'sourcing_id={r["id"]} 도서 차단 (productType={product_type})',
                )
                logger.warning(f'[promote-job {job_id}] 도서 차단: ASIN={asin} kw={book_kw}')
                blocked_violations += 1
                continue

            # ── 클린 정책 입구 게이트 ──
            blocked, kw = clean_policy.check_prohibited_ingredients(
                title_en or '', '', description_en or '',
            )
            if blocked:
                clean_policy.log_violation(
                    stage='sourcing', violation_type='prohibited_ingredient',
                    action_taken='blocked', matched_keyword=kw,
                    asin=asin, original_text=title_en,
                    notes=f'sourcing_id={r["id"]} 금지 성분 "{kw}"',
                )
                logger.warning(f'[promote-job {job_id}] 금지 성분 차단: ASIN={asin} kw={kw} title={title_en[:50]}')
                blocked_violations += 1
                continue

            # ── DTC 유전자검사 키트 영구 차단 (생명윤리법 제49조1항) ──
            # 2026-06-02 버그수정: 2번째 인자 r.get('title_ko')→title_en. r 은 sqlite3.Row
            # (.get 없음·title_ko 컬럼없음)라 promote 즉시크래시(온보딩 stall 근원). 영문 1차검사, 한글은 후속단계.
            gk_blocked, gk_kw = clean_policy.check_prohibited_genetic_kit(title_en, title_en)
            if gk_blocked:
                clean_policy.log_violation(
                    stage='sourcing', violation_type='dtc_genetic_kit',
                    action_taken='blocked', matched_keyword=gk_kw,
                    asin=asin, original_text=title_en,
                    notes=f'sourcing_id={r["id"]} DTC 유전자검사 키트 차단 "{gk_kw}"',
                )
                logger.warning(f'[promote-job {job_id}] 유전자검사 키트 차단: ASIN={asin} kw={gk_kw}')
                blocked_violations += 1
                continue

            # ── 의약외품 차단 (약사법 — 2026-06-13) ──
            qd_blocked, qd_kw = clean_policy.check_quasi_drug(None, title_en)
            if qd_blocked:
                clean_policy.log_violation(
                    stage='sourcing', violation_type='quasi_drug',
                    action_taken='blocked', matched_keyword=qd_kw,
                    asin=asin, original_text=title_en,
                    notes=f'sourcing_id={r["id"]} 의약외품 차단 "{qd_kw}" (약사법)',
                )
                logger.warning(f'[promote-job {job_id}] 의약외품 차단: ASIN={asin} kw={qd_kw}')
                blocked_violations += 1
                continue

            # ── 의류·신발 임시 차단 (PA_DISABLE_APPAREL_SHOES_BLOCK=1 로 해제) ──
            ap_blocked, ap_kw = clean_policy.check_blocked_apparel_shoes(title_en)
            if ap_blocked:
                clean_policy.log_violation(
                    stage='sourcing', violation_type='apparel_shoes_blocked',
                    action_taken='blocked', matched_keyword=ap_kw,
                    asin=asin, original_text=title_en,
                    notes=f'sourcing_id={r["id"]} 의류·신발 임시 차단 "{ap_kw}"',
                )
                logger.warning(f'[promote-job {job_id}] 의류·신발 차단: ASIN={asin} kw={ap_kw}')
                blocked_violations += 1
                continue

            # ── 전기용품 차단 (KC 전기안전인증 필요 — 2026-06-03) ──
            el_blocked, el_kw = clean_policy.check_electric_appliance(title_en)
            if el_blocked:
                clean_policy.log_violation(
                    stage='sourcing', violation_type='electric_appliance',
                    action_taken='blocked', matched_keyword=el_kw,
                    asin=asin, original_text=title_en,
                    notes=f'sourcing_id={r["id"]} 전기용품 차단 "{el_kw}" (KC 전기안전인증)',
                )
                logger.warning(f'[promote-job {job_id}] 전기용품 차단: ASIN={asin} kw={el_kw}')
                blocked_violations += 1
                continue

            # ── 식약처 8조 1호 + 약사법 hard block (safety_filter Tier 1~6) ──
            # 2026-05-19: 5/18 disease-name 사고 재발 방지. is_banned_diet_product 는
            # safety_filter 에 있었지만 호출처가 없었음. promote 단계에서 1차 차단.
            sf_match = safety_filter.is_banned_diet_product(title_en or '', '')
            if sf_match:
                clean_policy.log_violation(
                    stage='sourcing', violation_type='safety_filter',
                    action_taken='blocked', matched_keyword=sf_match,
                    asin=asin, original_text=title_en,
                    notes=f'sourcing_id={r["id"]} safety_filter: {sf_match}',
                )
                logger.warning(
                    f'[promote-job {job_id}] safety_filter 차단: ASIN={asin} '
                    f'rule={sf_match} title={(title_en or "")[:50]}'
                )
                blocked_violations += 1
                continue

            # cost_usd=0/None 차단 — downstream(마진 계산/등록) 안전성
            if cost_usd is None or cost_usd <= 0:
                clean_policy.log_violation(
                    stage='sourcing', violation_type='cost_zero_skip',
                    action_taken='blocked', matched_keyword=None,
                    asin=asin, original_text=title_en,
                    notes=(
                        f'sourcing_id={r["id"]} cost_usd={cost_usd} '
                        f'amazon_price_usd={amazon_price_usd} sheet_price_usd={r["price_usd"]} '
                        f'sheet_price_krw={r["price_krw"]}'
                    ),
                )
                logger.warning(
                    f'[promote-job {job_id}] cost_usd 비정상 차단: ASIN={asin} '
                    f'cost={cost_usd} amazon={amazon_price_usd} sheet_usd={r["price_usd"]}',
                )
                blocked_violations += 1
                continue

            # ── 한국 제조사 차단 (IP 라이선스 위반 회피) ──
            # 2026-05-20: manufacturer_classifier 존재했으나 wiring 누락. 신규 brand
            # 만나면 Naver+Gemini 분류 → 한국 회사면 차단. batch 내 같은 brand 캐시.
            if brand:
                if brand not in korean_mfr_cache:
                    try:
                        cls = manufacturer_classifier.classify_korean_sync(brand)
                        korean_mfr_cache[brand] = bool(cls and cls.get('is_korean'))
                    except Exception as e:
                        logger.warning(f'[promote-job {job_id}] mfr classify 예외: brand={brand} err={e}')
                        korean_mfr_cache[brand] = False  # 보수적 — 분류 실패 시 통과
                if korean_mfr_cache[brand]:
                    clean_policy.log_violation(
                        stage='sourcing', violation_type='korean_manufacturer',
                        action_taken='blocked', matched_keyword=brand,
                        asin=asin, original_text=title_en,
                        notes=f'sourcing_id={r["id"]} brand={brand} 한국 회사 분류',
                    )
                    logger.warning(
                        f'[promote-job {job_id}] 한국 제조사 차단: ASIN={asin} brand={brand}'
                    )
                    blocked_violations += 1
                    continue

            # ── 변형 발굴 (SP-API relationships) → parent_asin / is_group_master ──
            # 단독=None / 부모=self / 자식=부모ASIN. channelsending 이 이 컬럼으로
            # 3케이스 라우팅(단독→쿠팡단일 / 변형→group_registration_queue).
            parent_asin = sp.get("parent_asin")
            is_group_master = 1 if sp.get("is_parent") else 0

            prepared.append((
                r["id"], asin, title_en, description_en, brand,
                cost_usd, images_json, amazon_price_usd,
                dimensions_json, weight_g, identifiers_json, amazon_category_json,
                parent_asin, is_group_master,
            ))
            if sp:
                enriched += 1

            # 진행률 갱신 — 10건마다 또는 마지막 항목 (lock contention 회피)
            if idx % 10 == 0 or idx == total:
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
                    brand, cost_usd, images_json, amazon_price_usd,
                    dimensions_json, weight_g, identifiers_json, amazon_category_json,
                    parent_asin, is_group_master, status)
                   VALUES (?, 'purchase', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft')""",
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
        logger.info(f"[promote-job {job_id}] {promoted}건 이관, {enriched}건 SP-API 보강, {blocked_violations}건 차단, {errors}건 오류")

        # result_json: sheet_queue_worker 가 'new' 키를 promoted 카운트로 사용.
        # 워커 chain 이 promoted=0 일 때 다음 단계를 skip 하므로 필수.
        result_json = json.dumps({
            "new": promoted,
            "enriched": enriched,
            "blocked": blocked_violations,
            "errors": errors,
            # 2026-06-02: promote 가 기존 products ASIN 대조해 skip 한 수 (중복 product 차단)
            "duplicate_skipped": duplicate_skipped,
        })

        with get_db() as conn:
            conn.execute(
                """UPDATE batch_jobs
                   SET status='done', processed=?, errors=?, finished_at=?,
                       phase_message=?, result_json=?
                   WHERE id=?""",
                (
                    promoted,
                    errors,
                    _now_iso(),
                    f"완료 — {promoted}건 이관, {enriched}건 SP-API 보강, {blocked_violations}건 차단(금지성분)",
                    result_json,
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
