"""
keyword_to_groups.py — 키워드 → SP-API search → 부모 그룹 수집 → 멀티옵션 쿠팡 등록.

전체 파이프라인 단계:
  1. search_catalog_items(keyword)          # SP-API 검색, 최대 20 ASIN
  2. relationships → unique 부모 ASIN 추출    # 자식이면 부모, 단독이면 자기자신
  3. resync_group_from_spapi(parent)         # discover_group(SP-API 진실 동기화) + fetch_and_insert_children
  4. register_new_group_listing(parent)      # 멀티옵션 등록 (dry_run 가능)
     - build_coupang_payload 가:
       · 변형차원→카테고리 옵션축 매핑(갭②) + 부분일치 + 스펙denylist
       · 옵션축 값 고유성 검사(갭①) → 붕괴 시 skip
       · 색상/재질/수량 등 8개 axes 정규화(_extract_option_values)
       · 옵션명·속성값 한글화(korean_label) + 공통접미사 제거 + size 오염 제거

단독상품(variation 없음)은 그룹 등록 불가 → 'no_variation' 으로 skip(향후 단일 path 분기 가능).
KC/CaseC/색상비-mandatory 카테고리는 build 단계에서 자동 skip(기존 게이트).
"""
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

SP_API_DELAY = 0.6        # 카테고리 검색 사이 안전 간격(보호적)
US_MARKETPLACE_ID = "ATVPDKIKX0DER"


async def translate_keyword_to_english(keyword: str) -> str:
    """한글 키워드 → 영어 번역 (translate_text 캐시 활용). 영문/숫자는 그대로 통과."""
    if not keyword or not any('가' <= ch <= '힣' for ch in keyword):
        return keyword
    try:
        from backend_shared.ai.service import translate_text
        res = await translate_text(keyword, source_lang="ko", target_lang="en",
                                   context="Amazon US product search keyword")
        out = (res or {}).get("translated") or ""
        return out.strip() or keyword
    except Exception as e:
        logger.warning(f"[kw2groups] 번역 실패 '{keyword}': {e} — 원문 사용")
        return keyword


def search_keyword_asins(keyword: str, marketplace: str = "US",
                         page_size: int = 20) -> list[dict]:
    """SP-API search_catalog_items 호출. 각 항목: {asin, parent_asin, title, bsr}.
    parent_asin: 자식이면 부모 ASIN, 단독이면 self ASIN."""
    try:
        from sp_api.api import CatalogItems
        from sp_api.base import Marketplaces
        from backend.dropshipping.services.amazon_sp_api_service import get_credentials
    except ImportError as e:
        logger.warning(f"[kw2groups] sp_api 모듈 import 실패: {e}")
        return []

    mp_obj = getattr(Marketplaces, marketplace, Marketplaces.US)
    mp_id = US_MARKETPLACE_ID  # 현재 US 만 지원
    cat = CatalogItems(credentials=get_credentials(), marketplace=mp_obj, version="2022-04-01")

    try:
        resp = cat.search_catalog_items(
            keywords=[keyword],
            marketplaceIds=[mp_id],
            includedData=["summaries", "relationships", "salesRanks"],
            pageSize=min(page_size, 20),
        )
    except Exception as e:
        logger.warning(f"[kw2groups] search '{keyword}' 실패: {e}")
        return []

    items = (resp.payload or {}).get("items", []) or []
    out = []
    for it in items:
        asin = it.get("asin")
        if not asin:
            continue
        # 부모 추출 (variation child → 부모, 단독 → self)
        parent = None
        for entry in (it.get("relationships") or []):
            for r in (entry.get("relationships") or []):
                if r.get("type") == "VARIATION" and r.get("parentAsins"):
                    parent = r["parentAsins"][0]
                    break
            if parent:
                break
        title = ((it.get("summaries") or [{}])[0].get("itemName") or "")[:80]
        # 첫 sales rank
        bsr = None
        for sr in (it.get("salesRanks") or []):
            crs = sr.get("classificationRanks") or sr.get("displayGroupRanks") or []
            if crs:
                bsr = crs[0].get("rank")
                break
        out.append({"asin": asin, "parent_asin": parent or asin,
                    "is_variation_child": bool(parent), "title": title, "bsr": bsr})
    return out


def collect_unique_parents(search_results: list[dict]) -> list[str]:
    """search 결과 → 중복 제거된 부모 ASIN 리스트 (입력 순서 유지)."""
    seen = []
    for r in search_results:
        p = r.get("parent_asin")
        if p and p not in seen:
            seen.append(p)
    return seen


def _run_ai_detailing_for_group(parent_asin: str, platform: str = "coupang") -> int:
    """그룹 자식 products → 기존 AI 파이프라인(run_two_stage_batch) Stage1+Stage2 실행.

    sheet_queue_worker 의 detailing 단계와 동일한 호출 패턴. title_ko/description_ko/
    seo_tags/images 등을 단일경로와 일관되게 채움. 단순 translate_text 보다 무거우나
    품질이 검증된 풀세트.

    반환: 처리 대상 product 수 (0 = 자식 없음 / 모듈 import 실패).
    """
    import os as _os0
    if _os0.environ.get("PA_SKIP_GEMINI") == "1":
        return 0
    import json as _json
    import uuid as _uuid
    import asyncio as _asyncio
    from datetime import datetime as _dt, timezone as _tz
    from backend.purchase.database import get_db

    # ★ backend_shared.context 의 db_factory 가 미등록이면 등록 (스크립트/CLI 진입 대비).
    # API 프로세스는 기동 시 register_db_factory(get_db) 를 호출하지만,
    # nohup python 스크립트는 그 단계가 없어 Stage 2 의 translate_text 가 silent fallback.
    try:
        from backend_shared.context import register_db_factory, get_db as _shared_get_db
        try:
            with _shared_get_db() as _c:
                pass
        except RuntimeError:
            register_db_factory(get_db)
    except Exception as _e:
        logger.warning(f"[ai-detail] db_factory 등록 시도 실패(계속): {_e}")

    # 1) 자식 ASIN → 우리 products.id 수집
    with get_db() as conn:
        vg = conn.execute(
            "SELECT child_asins_json FROM variation_groups WHERE parent_asin=?",
            (parent_asin,),
        ).fetchone()
    if not vg or not vg["child_asins_json"]:
        return 0
    try:
        asins = _json.loads(vg["child_asins_json"])
    except Exception:
        return 0
    if not asins:
        return 0
    with get_db() as conn:
        ph = ",".join("?" * len(asins))
        pids = [r["id"] for r in conn.execute(
            f"SELECT id FROM products WHERE asin IN ({ph})", asins
        ).fetchall()]
    if not pids:
        return 0

    # 2) AI batch 호출 (sheet_queue_worker 와 동일 패턴)
    try:
        from backend.purchase.services.ai_processor import run_two_stage_batch
    except ImportError as e:
        logger.warning(f"[kw2groups] run_two_stage_batch import 실패: {e}")
        return 0

    job_id = _uuid.uuid4().hex[:12]
    now_iso = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO batch_jobs (id, job_type, status, total, created_at, parent_asin)
               VALUES (?, 'ai_detail', 'pending', ?, ?, ?)""",
            (job_id, len(pids), now_iso, parent_asin),
        )
    try:
        _asyncio.run(run_two_stage_batch(job_id, pids, platform=platform))
    except Exception as e:
        logger.warning(f"[kw2groups] AI detailing {parent_asin} job={job_id} 부분 실패: {e}")
    return len(pids)


def process_keyword(keyword: str, *, marketplace: str = "US",
                    max_groups: int = 5, channels: Optional[list[str]] = None,
                    dry_run: bool = True, requested: bool = True) -> dict:
    """키워드 1개 → 그룹 등록 end-to-end.

    dry_run=True(기본): build 페이로드까지만, 실제 POST 안 함.
    dry_run=False: register_product 까지 실행(라이브 등록).
    requested=True: 등록과 동시 자동 승인요청(심사대기열). False: 임시저장(셀러센터에서 검토·삭제 가능).
    """
    from backend.purchase.services.group_lister import (
        register_new_group_listing,
    )

    channels = channels or ["coupang"]
    out: dict = {"keyword": keyword, "dry_run": dry_run, "channels": channels,
                 "groups": [], "summary": {}}

    # 1) 키워드 검색
    sr = search_keyword_asins(keyword, marketplace=marketplace)
    if not sr:
        out["error"] = "search 결과 0건"
        return out
    out["search_total"] = len(sr)
    out["variation_children"] = sum(1 for x in sr if x["is_variation_child"])

    # 2) 부모 dedupe + 상위 max_groups
    parents = collect_unique_parents(sr)[:max_groups]
    out["parents_picked"] = len(parents)

    # 3) 각 부모 — resync + (dry_run 가능) 등록
    n_ok = n_skip = n_err = 0
    for idx, p in enumerate(parents):
        if idx > 0:
            time.sleep(SP_API_DELAY)
        rec: dict = {"parent_asin": p}
        try:
            # register_new_group_listing 가 진입부에서 resync_group_from_spapi 호출함.
            # 단 resync 가 자식을 적재한 뒤에 title 번역이 필요하므로 여기서 한 번 더 명시 호출.
            from backend.purchase.services.group_lister import resync_group_from_spapi
            r_err = resync_group_from_spapi(p)
            if r_err:
                rec["status"] = f"skip: {r_err}"
                n_skip += 1
                out["groups"].append(rec)
                continue
            n_detailed = _run_ai_detailing_for_group(p, platform=(channels[0] if channels else "coupang"))
            rec["ai_detailed"] = n_detailed
            reg = register_new_group_listing(p, channels=channels, dry_run=dry_run, requested=requested)
            if isinstance(reg, dict) and reg.get("error"):
                rec["status"] = f"skip: {reg['error']}"
                n_skip += 1
            else:
                rec["status"] = "ok"
                rec["channels"] = (reg or {}).get("channels", {})
                n_ok += 1
        except Exception as e:
            rec["status"] = f"error: {type(e).__name__}: {e}"
            n_err += 1
            logger.exception(f"[kw2groups] {p} 처리 중 예외")
        out["groups"].append(rec)

    out["summary"] = {"ok": n_ok, "skip": n_skip, "error": n_err}
    return out
