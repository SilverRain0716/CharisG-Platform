"""
sp_api_group_discovery.py — parent_asin → childAsins 묶음 발견 (lightweight).

목적:
  Phase 1 백필로 6,098 products 가 parent_asin 보유. 이 parent ASIN 들에 대해
  SP-API CatalogItems 1회 호출로 childAsins 리스트 + variation_theme + 변형 차원
  메타만 받아 variation_groups 캐시. 실제 child 별 facts 는 등록 시점 lazy load.

이 단계의 목표는 "어느 group 이 30/100 한도 초과인지" 식별 가능하도록
child_count 와 variation_dimensions 를 미리 채워두는 것.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# CatalogItems rate limit (sp_api_facts.py 와 동일 정책)
_SP_API_INTERVAL_SEC = 0.5
_last_call_ts = 0.0


def _rate_limit_wait() -> None:
    global _last_call_ts
    now = time.monotonic()
    elapsed = now - _last_call_ts
    if elapsed < _SP_API_INTERVAL_SEC:
        time.sleep(_SP_API_INTERVAL_SEC - elapsed)
    _last_call_ts = time.monotonic()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _call_parent_catalog(parent_asin: str, marketplace: str = "US") -> Optional[dict]:
    """parent ASIN 1건 SP-API 호출. 응답 raw payload 반환."""
    try:
        from sp_api.api import CatalogItems
        from sp_api.base import Marketplaces
        from backend.dropshipping.services.amazon_sp_api_service import get_credentials
    except ImportError as e:
        logger.warning(f"sp_api 모듈 없음: {e}")
        return None

    mp_id = {"US": "ATVPDKIKX0DER", "CA": "A2EUQ1WTGCTBG2", "MX": "A1AM78C64UM0Y8"}.get(
        marketplace, "ATVPDKIKX0DER"
    )

    _rate_limit_wait()
    try:
        creds = get_credentials()
        catalog = CatalogItems(credentials=creds, marketplace=getattr(Marketplaces, marketplace, Marketplaces.US), version="2022-04-01")
        resp = catalog.get_catalog_item(
            asin=parent_asin,
            includedData=["summaries", "relationships"],
            marketplaceIds=[mp_id],
        )
        return resp.payload or {}
    except Exception as e:
        logger.warning(f"[group-discovery] parent {parent_asin} 호출 실패: {e}")
        return None


def _extract_group_meta(parent_asin: str, item: dict) -> Optional[dict]:
    """SP-API 응답 → variation_groups row 매핑 dict.

    childAsins / variation_theme / variation_dimensions 추출.
    """
    if not isinstance(item, dict):
        return None
    summaries = item.get("summaries") or []
    s0 = summaries[0] if summaries else {}
    relationships = item.get("relationships") or []

    child_asins: list[str] = []
    theme = None
    dims: list[str] = []
    for entry in relationships:
        if not isinstance(entry, dict):
            continue
        for rel in entry.get("relationships") or []:
            if not isinstance(rel, dict):
                continue
            if rel.get("type") != "VARIATION":
                continue
            ch = rel.get("childAsins") or []
            if isinstance(ch, list):
                child_asins.extend(str(c) for c in ch if c)
            vt = rel.get("variationTheme") or {}
            if isinstance(vt, dict):
                if vt.get("theme"):
                    theme = vt["theme"]
                if vt.get("attributes"):
                    dims = list(vt["attributes"])
    # dedupe
    child_asins = list(dict.fromkeys(child_asins))

    return {
        "parent_asin": parent_asin,
        "variation_theme": theme,
        "variation_dimensions": dims,
        "child_asins": child_asins,
        "brand": s0.get("brand") if isinstance(s0, dict) else None,
        "item_name": s0.get("itemName") if isinstance(s0, dict) else None,
    }


def _persist_group(meta: dict) -> None:
    """variation_groups 테이블에 INSERT OR REPLACE."""
    try:
        from backend.purchase.database import get_db
    except ImportError:
        return
    parent_asin = meta["parent_asin"]
    with get_db() as conn:
        conn.execute(
            """INSERT INTO variation_groups
               (parent_asin, variation_theme, variation_dimensions,
                child_asins_json, child_count, brand, base_name_en,
                ingestion_status, discovered_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'discovered', ?)
               ON CONFLICT(parent_asin) DO UPDATE SET
                 variation_theme = excluded.variation_theme,
                 variation_dimensions = excluded.variation_dimensions,
                 child_asins_json = excluded.child_asins_json,
                 child_count = excluded.child_count,
                 brand = COALESCE(excluded.brand, variation_groups.brand),
                 base_name_en = COALESCE(excluded.base_name_en, variation_groups.base_name_en),
                 discovered_at = excluded.discovered_at""",
            (
                parent_asin,
                meta.get("variation_theme"),
                json.dumps(meta.get("variation_dimensions") or [], ensure_ascii=False),
                json.dumps(meta.get("child_asins") or [], ensure_ascii=False),
                len(meta.get("child_asins") or []),
                meta.get("brand"),
                meta.get("item_name"),
                _now_iso(),
            ),
        )


def discover_group(parent_asin: str, marketplace: str = "US") -> Optional[dict]:
    """단일 parent ASIN → variation_groups upsert.

    반환: meta dict (parent_asin, variation_theme, child_asins, ...) 또는 None.
    """
    if not parent_asin:
        return None
    parent_asin = parent_asin.strip().upper()
    item = _call_parent_catalog(parent_asin, marketplace)
    if item is None:
        return None
    meta = _extract_group_meta(parent_asin, item)
    if not meta:
        return None
    try:
        _persist_group(meta)
    except Exception as e:
        logger.warning(f"[group-discovery] persist 실패 {parent_asin}: {e}")
    return meta


def collect_unique_parents() -> list[str]:
    """products 에서 unique parent_asin 목록 — variation_groups 미수집 대상."""
    try:
        from backend.purchase.database import get_db
    except ImportError:
        return []
    with get_db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT parent_asin FROM products
               WHERE parent_asin IS NOT NULL AND parent_asin != ''
                 AND parent_asin NOT IN (SELECT parent_asin FROM variation_groups)
               ORDER BY parent_asin"""
        ).fetchall()
    return [r["parent_asin"] for r in rows]
