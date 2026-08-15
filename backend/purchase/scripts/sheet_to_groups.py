"""
sheet_to_groups.py — 시트 ASIN → 부모 그룹화 → 멀티옵션 등록 (방식 B).

기존 sheet_queue_worker (시트→단일상품 1개씩 등록) 와 별개 경로.
시트의 ASIN들을 SP-API 로 부모 dedupe → 부모마다 register_new_group_listing 호출 →
멀티옵션 1상품으로 등록. 부모의 형제 변형은 resync_group_from_spapi 가 자동 보강.

차단 게이트는 register_new_group_listing 진입부에서 적용됨
(한국 제조사·금지 성분·DTC 유전자검사·의류신발·KC 비면제).

사용:
    .venv/bin/python -m backend.purchase.scripts.sheet_to_groups \\
        --sheet-url "https://docs.google.com/spreadsheets/d/..." \\
        --tab "스노클링" --limit 5 --dry-run

    .venv/bin/python -m backend.purchase.scripts.sheet_to_groups \\
        --sheet-url "..." --tab "스노클링" --limit 5         # 라이브, requested=False(임시저장)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Optional

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("sheet-to-groups")

SP_API_DELAY = 0.5
US_MARKETPLACE_ID = "ATVPDKIKX0DER"


def get_parent_asin(asin: str) -> Optional[str]:
    """단일 ASIN → parent. variation child 면 부모, 단독이면 self ASIN. 실패 시 None."""
    try:
        from sp_api.api import CatalogItems
        from sp_api.base import Marketplaces
        from backend.dropshipping.services.amazon_sp_api_service import get_credentials
    except ImportError as e:
        logger.warning(f"sp_api 모듈 import 실패: {e}")
        return None
    try:
        cat = CatalogItems(credentials=get_credentials(),
                           marketplace=Marketplaces.US, version="2022-04-01")
        resp = cat.get_catalog_item(asin=asin, includedData=["relationships"],
                                    marketplaceIds=[US_MARKETPLACE_ID])
        item = resp.payload or {}
        for entry in item.get("relationships") or []:
            for r in entry.get("relationships") or []:
                if r.get("type") == "VARIATION" and r.get("parentAsins"):
                    return r["parentAsins"][0]
        return asin  # 단독상품
    except Exception as e:
        logger.warning(f"  asin={asin} parent 조회 실패: {e}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet-url", required=True)
    ap.add_argument("--tab", required=True, help="처리할 탭 이름 (예: 스노클링)")
    ap.add_argument("--limit", type=int, default=5, help="처리할 부모 그룹 상한 (기본 5)")
    ap.add_argument("--dry-run", action="store_true", help="빌드만, 쿠팡 POST 안 함")
    ap.add_argument("--requested", action="store_true",
                    help="자동승인요청. 기본 False=임시저장(셀러센터 검토 가능, 권장)")
    ap.add_argument("--max-splits", type=int, default=8,
                    help="부모당 split 상한. 초과 부모는 pre-scan 에서 제외 (기본 8)")
    ap.add_argument("--pre-scan-only", action="store_true",
                    help="등록 안 함 — 부모별 splits 통계만 출력")
    ap.add_argument("--scan-budget", type=int, default=0,
                    help="pre-scan 대상 부모 수 (0=무제한, 기본 limit*4)")
    args = ap.parse_args()

    from backend.purchase.services.sheet_importer import (
        extract_sheet_id, discover_tabs, fetch_tab_csv,
    )
    from backend.purchase.services.group_lister import register_new_group_listing
    from backend.purchase.services.variation import load_group, auto_split

    sid = extract_sheet_id(args.sheet_url)
    if not sid:
        logger.error("sheet_id 추출 실패")
        return 1

    tabs, _ = discover_tabs(sid)
    target = next((t for t in tabs if t.get("name") == args.tab), None)
    if not target:
        logger.error(f"탭 '{args.tab}' 없음. 가능: {[t.get('name') for t in tabs]}")
        return 1

    rows = fetch_tab_csv(sid, int(target["gid"]))
    asins = []
    for r in rows:
        a = (r.get("ASIN") or "").strip().upper()
        if len(a) == 10 and a.isalnum():
            asins.append(a)
    logger.info(f"시트 '{args.tab}' → 유효 ASIN {len(asins)}개")

    # ── 단계 1) SP-API 로 부모 dedupe (scan_budget 까지) ──
    scan_budget = args.scan_budget or (args.limit * 4)
    seen_parents: list[str] = []
    seen_set: set[str] = set()
    for i, asin in enumerate(asins):
        if i > 0:
            time.sleep(SP_API_DELAY)
        p = get_parent_asin(asin)
        if p and p not in seen_set:
            seen_set.add(p)
            seen_parents.append(p)
        if len(seen_parents) >= scan_budget:
            logger.info(f"  → 후보 부모 {scan_budget}개 도달 (ASIN {i+1}/{len(asins)} 스캔)")
            break
    logger.info(f"고유 부모 후보 {len(seen_parents)}개 → pre-scan (max-splits={args.max_splits})")

    # ── 단계 2) pre-scan: 부모마다 splits 시뮬 → 통과한 것만 후보 ──
    eligible: list[dict] = []
    rejected: list[dict] = []
    for p in seen_parents:
        try:
            g = load_group(p)
            if not g:
                rejected.append({"parent": p, "reason": "variation_groups 미적재 (resync 필요)"})
                continue
            splits = auto_split(g, "coupang")
            n = len(splits)
            primary = splits[0].get("split_dim") if splits else None
            child_count = len(g.get("children") or [])
            row = {"parent": p, "n_splits": n, "primary_dim": primary, "children": child_count}
            if n == 0:
                rejected.append({**row, "reason": "auto_split=0"})
            elif n > args.max_splits:
                rejected.append({**row, "reason": f"splits 폭발({n} > {args.max_splits})"})
            else:
                eligible.append(row)
        except Exception as e:
            rejected.append({"parent": p, "reason": f"{type(e).__name__}: {e}"})

    logger.info(f"pre-scan 결과: 통과 {len(eligible)} / 부적합 {len(rejected)}")
    for r in eligible[:10]:
        logger.info(f"  ✓ {r['parent']}  splits={r['n_splits']} dim={r['primary_dim']} children={r['children']}")
    for r in rejected[:10]:
        logger.info(f"  ✗ {r['parent']}  {r.get('reason')}")

    if args.pre_scan_only:
        logger.info("--pre-scan-only — 등록 스킵")
        return 0

    # 통과 후보 중 상위 limit 개만 처리
    seen_parents = [r["parent"] for r in eligible[: args.limit]]
    logger.info(f"실제 처리 부모 {len(seen_parents)}개 "
                f"(dry_run={args.dry_run}, requested={args.requested})")

    ok = skip = err = 0
    summaries: list[dict] = []
    for i, p in enumerate(seen_parents, 1):
        if i > 1:
            time.sleep(SP_API_DELAY)
        logger.info(f"[{i}/{len(seen_parents)}] parent={p}")
        try:
            res = register_new_group_listing(
                p, channels=["coupang"],
                dry_run=args.dry_run, requested=args.requested,
            )
            if isinstance(res, dict) and res.get("error"):
                logger.info(f"  → skip: {res['error'][:140]}")
                skip += 1
                summaries.append({"parent": p, "status": "skip", "reason": res["error"][:140]})
            else:
                ch = (res or {}).get("channels", {}).get("coupang") or []
                first = next((x for x in (ch if isinstance(ch, list) else [ch])
                              if isinstance(x, dict) and x.get("status")), None)
                logger.info(f"  → ok: {first}")
                ok += 1
                summaries.append({"parent": p, "status": "ok", "channel_result": first})
        except Exception as e:
            logger.exception(f"  → error: {type(e).__name__}")
            err += 1
            summaries.append({"parent": p, "status": "error", "exc": f"{type(e).__name__}: {e}"})

    logger.info(f"\n=== 완료 — ok={ok}, skip={skip}, error={err} (총 {len(seen_parents)}) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
