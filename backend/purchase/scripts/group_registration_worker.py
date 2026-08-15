"""
group_registration_worker — 부모 단위 그룹/멀티옵션 등록 직렬 워커.

흐름:
  1. group_registration_queue WHERE status='queued' 중 가장 오래된 1건 pick
  2. status='pre_scanning' → variation_groups 적재 확인 / resync_group_from_spapi
  3. auto_split → n_splits > 8 이면 skip
  4. status='registering' → register_new_group_listing 호출 (DRY_RUN 없음)
  5. 결과 → seller_product_ids, listing_ids, options_persisted 기록 + status='done'
  6. 실패 → status='error', error_message

폴링 60초. 동시 처리 금지 (t3.micro + DB 락 + AI rate).
일반 종료 시 SIGTERM 받아 진행 중 1 부모는 끝까지 처리 후 종료.

배포:
  systemd 서비스 charisg-pa-group-worker.service
  ExecStart=/home/ubuntu/CharisG-Platform/charisg-platform/.venv/bin/python -m backend.purchase.scripts.group_registration_worker
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from typing import Optional

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s group-worker: %(message)s",
)
logger = logging.getLogger("group-worker")

POLL_INTERVAL_SECS = 60
MAX_SPLITS_PER_GROUP = 8

# 멀티워커 샤딩 (2026-06-03): id % COUNT == INDEX 로 큐 행 공간 분할.
# 기본 1/0 → 단일워커 현행과 동일. 2개 파일럿: 워커A=2/0, 워커B=2/1.
_SHARD_COUNT = max(1, int(os.environ.get("GROUP_WORKER_SHARD_COUNT", "1")))
_SHARD_INDEX = int(os.environ.get("GROUP_WORKER_SHARD_INDEX", "0"))

_shutdown = False


def _sig_handler(signum, frame):
    global _shutdown
    logger.info(f"signal {signum} 수신 — 진행 중 작업 끝나면 종료")
    _shutdown = True


signal.signal(signal.SIGTERM, _sig_handler)
signal.signal(signal.SIGINT, _sig_handler)


def _pick_next() -> Optional[dict]:
    """status='queued' 중 (샤드 필터) 가장 오래된 1건 → 'pre_scanning' 로 atomic 전환.

    멀티워커(2026-06-03): GROUP_WORKER_SHARD_COUNT/INDEX 로 id 공간 분할
    (id % COUNT == INDEX) → 워커끼리 행 교집합 0 → 같은 그룹 중복등록 레이스 없음.
    기본 count=1, index=0 → `id % 1 == 0` 항상 참 → 단일워커 현행과 동일.
    """
    from backend.purchase.database import get_db
    with get_db() as conn:
        # 우선순위: 신규 시트/청소(=온보딩) 먼저, regroup(재그룹 캠페인)은 후순위.
        # regroup 은 신규 큐가 빌 때만 처리 → 신규 온보딩이 캠페인에 밀리지 않음.
        row = conn.execute(
            "SELECT * FROM group_registration_queue "
            "WHERE status='queued' AND (id % ?)=? "
            "ORDER BY (sheet_id LIKE 'regroup:%') ASC, id ASC LIMIT 1",
            (_SHARD_COUNT, _SHARD_INDEX),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE group_registration_queue SET status='pre_scanning', "
            "started_at=datetime('now') WHERE id=?",
            (row["id"],),
        )
        return dict(row)


def _mark_error(row_id: int, msg: str) -> None:
    from backend.purchase.database import get_db
    with get_db() as conn:
        conn.execute(
            "UPDATE group_registration_queue SET status='error', "
            "error_message=?, finished_at=datetime('now') WHERE id=?",
            (msg[:600], row_id),
        )


def _mark_skipped(row_id: int, reason: str, n_splits: int | None = None,
                   primary_dim: str | None = None, children_count: int | None = None) -> None:
    from backend.purchase.database import get_db
    with get_db() as conn:
        conn.execute(
            "UPDATE group_registration_queue SET status='skipped', "
            "skip_reason=?, n_splits=?, primary_dim=?, children_count=?, "
            "finished_at=datetime('now') WHERE id=?",
            (reason[:300], n_splits, primary_dim, children_count, row_id),
        )


def _mark_done_single(row_id: int, channel_product_id: str | None,
                        listing_id: int | None) -> None:
    """단독상품 fallback 등록 결과 마킹.

    cpid 있으면 done, 없으면 silent fail → skipped 로 분류 (정합성).
    """
    import json as _json
    from backend.purchase.database import get_db
    if not channel_product_id:
        _mark_skipped(row_id, "단독 fallback silent fail — cpid 미발급 (페이로드/이미지/등록 실패)",
                      0, "single", 1)
        return
    with get_db() as conn:
        conn.execute(
            "UPDATE group_registration_queue SET status='done', "
            "seller_product_ids=?, listing_ids=?, options_persisted=1, "
            "n_splits=0, primary_dim='single', children_count=1, "
            "finished_at=datetime('now') WHERE id=?",
            (
                _json.dumps([str(channel_product_id)]),
                _json.dumps([int(listing_id)] if listing_id else []),
                row_id,
            ),
        )


def _lookup_single_cpid_lid(parent_asin: str) -> tuple[str | None, int | None]:
    """단독 fallback 후 listings_pa.coupang 의 channel_product_id 와 listings_pa.id 조회.

    list_product 가 listings_pa UPDATE 했으므로 DB 가 진실 — sres 파싱보다 정확.
    """
    from backend.purchase.database import get_db
    with get_db() as conn:
        r = conn.execute(
            """SELECT l.id, l.channel_product_id FROM listings_pa l
               JOIN products p ON l.product_id = p.id
               WHERE p.asin=? AND l.channel='coupang' LIMIT 1""",
            (parent_asin,),
        ).fetchone()
    if not r:
        return None, None
    return r["channel_product_id"], r["id"]


def _register_single_fallback(parent_asin: str, requested: bool = False, discover_only: bool = False):
    """단독상품 등록 fallback — 변형 그룹 없는 ASIN 을 단일 path 로 등록.

    1) SP-API facts → products INSERT (없으면)
    2) cost_usd 책정 (BuyBox/Lowest)
    3) AI Stage1+Stage2 (title_ko 한글화)
    4) title_ko placeholder 차단 ([브랜드]/[브랜드명] prefix → 등록 거부)
    5) ensure_promoted (sale_price_krw + category)
    6) send_to_channels (listings_pa.coupang INSERT + sale_krw + 마진)
    7) coupang_lister.list_product (requested 전달)

    반환: (list_product_result_dict, error_str). 성공 시 (res, None).
    """
    import json as _json
    import uuid as _uuid
    import asyncio as _asyncio
    from backend.purchase.database import get_db
    try:
        from backend.purchase.services.sp_api_facts import fetch_full_catalog_facts
        from backend.purchase.services.group_lister import _get_buybox_or_lowest_price
        from backend.purchase.services.coupang_lister import list_product
    except ImportError as e:
        return None, f"import 실패: {e}"

    # 1) products row 확보 + cost 보강 (옛 draft 케이스 — products 있지만 cost_usd NULL)
    with get_db() as conn:
        r = conn.execute("SELECT id, cost_usd FROM products WHERE asin=?", (parent_asin,)).fetchone()
    if r:
        product_id = r["id"]
        if not r["cost_usd"] or float(r["cost_usd"] or 0) <= 0:
            try:
                price = _get_buybox_or_lowest_price(parent_asin)
            except Exception:
                price = None
            if price and price > 0:
                with get_db() as conn:
                    conn.execute("UPDATE products SET cost_usd=? WHERE id=?", (float(price), product_id))
                logger.info(f"[single-fallback] {parent_asin} cost 보강: ${price:.2f}")
            else:
                return None, f"cost 책정 불가 (BuyBox/Lowest 모두 없음) — 옛 draft"
    else:
        try:
            facts = fetch_full_catalog_facts(parent_asin)
        except Exception as e:
            return None, f"fetch_full_catalog_facts: {e}"
        if not facts:
            return None, "SP-API facts 없음"

        # ── 전자책/오디오북/디지털 콘텐츠 차단 (2026-08-01, 사장 지시) ──
        # 구매대행은 실물 배송이 본질 → 디지털은 처리 불가.
        # 기존엔 sourcing 경로에만 걸려 있어 CSV 임포트로는 그냥 통과했다.
        try:
            from backend.purchase.services import clean_policy as _cp
            _blocked, _kw = _cp.check_prohibited_book(
                facts.get("title_en") or "", facts.get("product_type") or "",
            )
            if _blocked:
                logger.info(f"[single-fallback] {parent_asin} 디지털 콘텐츠 차단 ({_kw})")
                return None, f"prohibited_digital:{_kw}"
        except ImportError:
            pass

        try:
            price = _get_buybox_or_lowest_price(parent_asin)
        except Exception:
            price = None
        cost_usd = float(price) if price and price > 0 else None
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO products (asin, title_en, brand, business_model, status,
                     sp_api_facts_json, sp_api_facts_at, weight_g, images_json, cost_usd)
                   VALUES (?, ?, ?, 'purchase', 'draft', ?, ?, ?, ?, ?)""",
                (
                    parent_asin,
                    facts.get("title_en") or "",
                    facts.get("brand") or facts.get("manufacturer"),
                    _json.dumps(facts, ensure_ascii=False),
                    facts.get("fetched_at"),
                    facts.get("item_weight_g") or facts.get("item_display_weight_g"),
                    _json.dumps(facts.get("images") or [], ensure_ascii=False),
                    cost_usd,
                ),
            )
            product_id = cur.lastrowid

    if discover_only:
        # ★발굴 전용(2026-07-24): products 행 확보까지만. AI/등록 스킵.
        return ({'discovered': True, 'product_id': product_id}, None)

    # 2) db_factory 등록 (nohup 진입 대비) + AI Stage1+Stage2
    try:
        from backend_shared.context import register_db_factory, get_db as _shared_get_db
        try:
            with _shared_get_db() as _c:
                pass
        except RuntimeError:
            register_db_factory(get_db)
    except Exception:
        pass

    try:
        from backend.purchase.services.ai_processor import run_two_stage_batch
        job_id = _uuid.uuid4().hex[:12]
        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO batch_jobs (id, status, phase, total) "
                "VALUES (?, 'running', 'two-stage', 1)",
                (job_id,),
            )
        _asyncio.run(run_two_stage_batch(job_id, [product_id], "coupang"))
    except Exception as e:
        logger.warning(f"[single-fallback] {parent_asin} AI 실패(계속): {e}")

    # 3) title_ko placeholder 차단 — 한글번역 실패 → 라이브 노출 시 검색 노이즈 큼
    with get_db() as conn:
        p_check = conn.execute(
            "SELECT title_ko FROM products WHERE id=?", (product_id,)
        ).fetchone()
    _tko = (p_check["title_ko"] if p_check else "") or ""
    if _tko.lstrip().startswith("[브랜드"):
        return None, f"title_ko placeholder ({_tko[:30]!r}) — 한글번역 실패, 라이브 차단"

    # 4) sale_price_krw / category / detail_pages 채우기
    try:
        from backend.purchase.services.group_lister import ensure_promoted
        ensure_promoted(product_id)
    except Exception as e:
        logger.warning(f"[single-fallback] {parent_asin} ensure_promoted 실패(계속): {e}")

    # 4.5) 이미지 다운로드 + image_cache 등록 (build_payload 의 image_count 검증용)
    try:
        from backend.purchase.services.image_downloader import download_product_images
        with get_db() as conn:
            _p = conn.execute("SELECT images_json FROM products WHERE id=?", (product_id,)).fetchone()
        if _p and _p["images_json"]:
            _asyncio.run(download_product_images(product_id, _p["images_json"]))
    except Exception as e:
        logger.warning(f"[single-fallback] {parent_asin} image download 실패(계속): {e}")

    # 5) listings_pa.coupang INSERT + sale_krw + 마진 계산 (cpid DB 추적 필수)
    try:
        from backend.purchase.services.channel_listing_service import send_to_channels
        send_to_channels(product_id, channels=["coupang"])
    except Exception as e:
        logger.warning(f"[single-fallback] {parent_asin} send_to_channels 실패(계속): {e}")

    # 6) 마진 게이트 — net_margin_krw < 15,000원 차단 (사용자 정책)
    try:
        from backend.purchase.services.margin_gate import block_listing_if_low_margin
        blocked, mreason = block_listing_if_low_margin(product_id)
        if blocked:
            return None, f"마진 차단: {mreason}"
    except Exception as e:
        logger.warning(f"[single-fallback] {parent_asin} margin gate 실패(계속): {e}")

    # 7) list_product (requested 명시 — 사용자 명시 승인 정책 준수)
    try:
        res = list_product(product_id, requested=bool(requested))
    except Exception as e:
        return None, f"list_product: {e}"
    return res, None


def _mark_done(row_id: int, res: dict, n_splits: int, primary_dim: str,
                children_count: int) -> None:
    from backend.purchase.database import get_db
    seller_ids: list[str] = []
    listing_ids: list[int] = []
    options_total = 0
    skip_reasons: list[str] = []
    ch_res = (res.get("channels") or {}).get("coupang") or []
    if isinstance(ch_res, list):
        for r in ch_res:
            if not isinstance(r, dict):
                continue
            if r.get("status") == "registered":
                if r.get("channel_product_id"):
                    seller_ids.append(str(r["channel_product_id"]))
                if r.get("listing_id"):
                    listing_ids.append(int(r["listing_id"]))
                if r.get("options_persisted"):
                    options_total += int(r["options_persisted"])
            else:
                reason = r.get("reason") or r.get("error") or r.get("status") or ""
                if reason:
                    skip_reasons.append(str(reason)[:100])
    # 등록 0건이면 silent fail — skipped 로 정합성 마킹
    if not seller_ids:
        msg = "; ".join(skip_reasons[:3]) or "등록 0건 — build/PUT 실패"
        _mark_skipped(row_id, msg[:240], n_splits, primary_dim, children_count)
        return
    with get_db() as conn:
        conn.execute(
            "UPDATE group_registration_queue SET status='done', "
            "seller_product_ids=?, listing_ids=?, options_persisted=?, "
            "n_splits=?, primary_dim=?, children_count=?, "
            "finished_at=datetime('now') WHERE id=?",
            (
                json.dumps(seller_ids),
                json.dumps(listing_ids),
                options_total,
                n_splits,
                primary_dim,
                children_count,
                row_id,
            ),
        )


def _is_corrupt_family(children: list) -> tuple[bool, str]:
    """오염 변형패밀리 판정 — 자식 brand 불일치(부분문자열 관계 아님)면 오염.

    아마존 카탈로그가 무관 상품을 한 variation 으로 묶은 케이스(브러시+향수 등) 탐지.
    부모 title 은 신뢰 불가(판매불가 컨테이너) — 자식 brand 가 진짜 신호.
    'OXO' vs 'OXO Good Grips' 같은 부분문자열은 동일 브랜드로 간주(오탐 회피).
    """
    brands = []
    for c in children or []:
        b = (c.get("brand") or c.get("manufacturer") or "").strip().lower()
        if b:
            brands.append(b)
    uniq = list(dict.fromkeys(brands))
    if len(uniq) <= 1:
        return False, ""
    def _related(a: str, b: str) -> bool:
        return a in b or b in a
    for i in range(len(uniq)):
        for j in range(i + 1, len(uniq)):
            if not _related(uniq[i], uniq[j]):
                return True, f"brand 불일치 {uniq}"
    return False, ""


def _coupang_registered_any(res: dict) -> bool:
    """register_new_group_listing 결과에 실제 등록된 쿠팡 split 이 있나."""
    ch = (res.get("channels") or {}).get("coupang") or []
    return any(isinstance(r, dict) and r.get("status") == "registered" for r in ch)


def _register_children_as_singles(children: list, requested: bool) -> tuple[int, int]:
    """그룹 등록 불가(오염/노이즈/빌드거부) 시 자식들을 각각 쿠팡 단일로 폴백.

    각 자식은 _register_single_fallback 로 처리 → clean_policy 가 자식별 필터
    (오염 패밀리의 부적합 자식은 여기서 차단됨).
    """
    ok, fail = 0, 0
    for c in children or []:
        asin = c.get("asin")
        if not asin:
            continue
        try:
            sres, serr = _register_single_fallback(asin, requested=requested)
            if serr or not sres or not sres.get("ok"):
                fail += 1
                logger.info(f"[child-single] {asin} skip/fail: {serr or (sres or {}).get('error','')}")
            else:
                ok += 1
                logger.info(f"[child-single] {asin} 단일 등록 ok")
        except Exception as e:
            fail += 1
            logger.warning(f"[child-single] {asin} 예외: {e}")
    return ok, fail


def _mark_done_singles(row_id: int, ok: int, fail: int, n_splits: int | None,
                        primary_dim: str | None, children_count: int | None,
                        note: str) -> None:
    """자식-단일 폴백 결과 마킹. status='done_singles' (그룹 아닌 단일들로 처리됨)."""
    from backend.purchase.database import get_db
    with get_db() as conn:
        conn.execute(
            "UPDATE group_registration_queue SET status='done_singles', "
            "skip_reason=?, n_splits=?, primary_dim=?, children_count=?, "
            "options_persisted=?, finished_at=datetime('now') WHERE id=?",
            (f"{note}: 단일 ok={ok} fail={fail}"[:300], n_splits, primary_dim,
             children_count, ok, row_id),
        )


def process_one() -> bool:
    """1 부모 처리. 처리한 게 있으면 True, queue 비었으면 False."""
    job = _pick_next()
    if not job:
        return False

    parent_asin = job["parent_asin"]
    requested = bool(job["requested"])
    # 안전모드(regroup): 기존 listed 상품 재그룹 — 그룹 불가 시 단일 재등록 생략(기존 단일 유지)
    is_regroup = str(job.get("sheet_id") or "").startswith("regroup:")
    logger.info(f"[{job['id']}] {parent_asin} 처리 시작 (requested={requested})")

    try:
        # pre-scan — variation_groups 미적재면 SP-API resync 자동
        from backend.purchase.services.variation import load_group, auto_split
        from backend.purchase.services.group_lister import resync_group_from_spapi
        g = load_group(parent_asin)
        if not g:
            err = resync_group_from_spapi(parent_asin)
            if err:
                # 단독상품 (변형 그룹 아님) → 단일 path fallback 자동 호출
                if err and "자식 ASIN 0개" in err:
                    if is_regroup:
                        _mark_skipped(job["id"], "regroup 단독전환(자식0) — 재등록 생략")
                        logger.info(f"[{job['id']}] {parent_asin} regroup 단독전환 — 기존 단일 유지")
                        return True
                    logger.info(f"[{job['id']}] {parent_asin} 단독상품 fallback 시작")
                    sres, serr = _register_single_fallback(parent_asin, requested=requested)
                    if serr:
                        _mark_skipped(job["id"], f"단독 fallback 실패: {serr}")
                        logger.warning(f"[{job['id']}] {parent_asin} fallback err: {serr}")
                        return True
                    if not sres or not sres.get("ok"):
                        reason = (sres or {}).get("error", "list_product ok=False")
                        _mark_skipped(job["id"], f"단독 fallback skip: {reason[:200]}")
                        logger.info(f"[{job['id']}] {parent_asin} single skip: {reason[:200]}")
                        return True
                    # 성공 — channel_product_id + listing_id 추출
                    result = sres.get("result") or {}
                    # ★버그수정(2026-06-02): list_product 성공응답은 spid 를 result["data"] 에 담음
                    # (coupang_lister:919). 기존엔 없는 키(channel_product_id/sellerProductId)에서
                    # 읽어 항상 None → 등록 성공인데도 "cpid 미발급 silent fail" 오기록 + DB orphan.
                    cpid = (result.get("data") or result.get("channel_product_id")
                            or result.get("sellerProductId"))
                    lid = sres.get("listing_id") or result.get("listing_id")
                    # 견고화: 응답에서 못 얻으면 listings_pa 재조회 (list_product 가 기록함)
                    if not cpid or not lid:
                        _db_cpid, _db_lid = _lookup_single_cpid_lid(parent_asin)
                        cpid = cpid or _db_cpid
                        lid = lid or _db_lid
                    _mark_done_single(job["id"], cpid, lid)
                    logger.info(f"[{job['id']}] {parent_asin} 단독 fallback done cpid={cpid}")
                    return True
                else:
                    _mark_error(job["id"], f"resync 실패: {err}")
                    logger.warning(f"[{job['id']}] resync 실패: {err}")
                return True
            g = load_group(parent_asin)
            if not g:
                _mark_error(job["id"], "load_group None (resync 후에도)")
                return True

        splits = auto_split(g, "coupang")
        n_splits = len(splits)
        primary_dim = splits[0].get("split_dim") if splits else None
        children_count = len(g.get("children") or [])

        if n_splits == 0:
            if children_count == 0:
                if is_regroup:
                    _mark_skipped(job["id"], "regroup 단독전환(children=0) — 재등록 생략",
                                  n_splits, primary_dim, children_count)
                    logger.info(f"[{job['id']}] {parent_asin} regroup 단독전환 — 기존 단일 유지")
                    return True
                logger.info(f"[{job['id']}] {parent_asin} 단독 fallback 시작 (group row 있으나 children=0)")
                sres, serr = _register_single_fallback(parent_asin, requested=requested)
                if serr:
                    _mark_skipped(job["id"], f"단독 fallback 실패: {serr}",
                                  n_splits, primary_dim, children_count)
                    logger.warning(f"[{job['id']}] {parent_asin} fallback err: {serr}")
                    return True
                if not sres or not sres.get("ok"):
                    reason = (sres or {}).get("error", "list_product ok=False")
                    _mark_skipped(job["id"], f"단독 fallback skip: {reason[:200]}",
                                  n_splits, primary_dim, children_count)
                    logger.info(f"[{job['id']}] {parent_asin} single skip: {reason[:200]}")
                    return True
                cpid, lid = _lookup_single_cpid_lid(parent_asin)
                _mark_done_single(job["id"], cpid, lid)
                logger.info(f"[{job['id']}] {parent_asin} 단독 fallback done cpid={cpid}")
                return True
            _mark_skipped(job["id"], "auto_split=0", n_splits, primary_dim, children_count)
            return True
        if n_splits > MAX_SPLITS_PER_GROUP:
            _mark_skipped(
                job["id"],
                f"splits {n_splits} > {MAX_SPLITS_PER_GROUP} — 그룹화 부적합",
                n_splits, primary_dim, children_count,
            )
            logger.info(f"[{job['id']}] {parent_asin} skipped: splits {n_splits}")
            return True

        # ★ 오염 가드 — 자식 brand 불일치(무관 상품 혼합) 시 그룹화 거부 → 자식 단일 폴백
        _children = g.get("children") or []
        _corrupt, _creason = _is_corrupt_family(_children)
        if _corrupt:
            if is_regroup:
                _mark_skipped(job["id"], f"regroup 오염({_creason}) — 단일 재등록 생략",
                              n_splits, primary_dim, children_count)
                logger.info(f"[{job['id']}] {parent_asin} regroup 오염 — 기존 단일 유지(재등록 생략)")
                return True
            logger.warning(f"[{job['id']}] {parent_asin} 오염 패밀리({_creason}) → 자식 단일 폴백")
            ok, fail = _register_children_as_singles(_children, requested)
            _mark_done_singles(job["id"], ok, fail, n_splits, primary_dim,
                               children_count, f"오염({_creason})")
            return True

        # 실제 등록
        from backend.purchase.database import get_db
        with get_db() as conn:
            conn.execute(
                "UPDATE group_registration_queue SET status='registering' WHERE id=?",
                (job["id"],),
            )
        from backend.purchase.services.group_lister import register_new_group_listing
        channels = [c.strip() for c in (job["channels"] or "coupang").split(",") if c.strip()]
        res = register_new_group_listing(
            parent_asin, channels=channels,
            dry_run=False, requested=requested,
        )
        if isinstance(res, dict) and res.get("error"):
            _mark_error(job["id"], res["error"])
            logger.warning(f"[{job['id']}] {parent_asin} error: {res['error'][:200]}")
            return True
        # 그룹 등록 0건(노이즈-drop/빌드거부) → 자식 단일 폴백 (커버리지 보존)
        if not _coupang_registered_any(res or {}):
            if is_regroup:
                _mark_skipped(job["id"], "regroup 그룹불가(노이즈/빌드거부) — 단일 재등록 생략",
                              n_splits, primary_dim, children_count)
                logger.info(f"[{job['id']}] {parent_asin} regroup 그룹불가 — 기존 단일 유지")
                return True
            logger.info(f"[{job['id']}] {parent_asin} 그룹 등록 0건 → 자식 단일 폴백")
            ok, fail = _register_children_as_singles(_children, requested)
            _mark_done_singles(job["id"], ok, fail, n_splits, primary_dim,
                               children_count, "노이즈/빌드거부")
            return True
        _mark_done(job["id"], res or {}, n_splits, primary_dim or "", children_count)
        logger.info(f"[{job['id']}] {parent_asin} done (splits={n_splits})")
        return True

    except Exception as e:
        _mark_error(job["id"], f"{type(e).__name__}: {e}")
        logger.exception(f"[{job['id']}] {parent_asin} 예외")
        return True


def main() -> int:
    # 예약식(타이머) 백필 모델: queue 를 drain 하면 종료(exit-when-empty).
    # → 빈 날은 ~1폴 만에 exit 0 (8h 헛폴링 + "failed(timeout)" cosmetic 제거).
    # GROUP_WORKER_DAEMON=1 이면 상시 폴링(구 동작) 유지.
    poll_forever = os.environ.get("GROUP_WORKER_DAEMON", "").strip().lower() in ("1", "true", "yes")
    logger.info(f"group-worker 시작 (poll={POLL_INTERVAL_SECS}s, mode={'daemon' if poll_forever else 'drain-exit'})")
    empty_streak = 0
    from backend.purchase.services.job_lock import shared_unit
    while not _shutdown:
        try:
            # ★ turnstile SHARED 락 — 패밀리 1개 동안 보유, 종료 시 release+yield.
            # 배치잡(EXCLUSIVE) 대기 시 여기서 블록 → 배치잡 우선, 패밀리 경계마다 양보.
            with shared_unit("group-worker"):
                picked = process_one()
        except Exception:
            logger.exception("process_one 예외")
            picked = False
        if picked:
            empty_streak = 0
            continue
        # queue 비었음
        empty_streak += 1
        if not poll_forever and empty_streak >= 2:
            logger.info("queue drain 완료 — 종료 (exit-when-empty)")
            break
        for _ in range(POLL_INTERVAL_SECS):  # 비었을 때만 polling 대기 (transient 대비 1회 유예)
            if _shutdown:
                break
            time.sleep(1)
    logger.info("group-worker 종료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
