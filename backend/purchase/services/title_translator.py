"""
title_translator.py — products.title_ko 자동 번역 helper.

영문 title 이 있고 한국어 title 이 비어있을 때 backend_shared.ai.translate_text
(translation_cache 캐시 적용) 로 보강. 카테고리 매핑/세부 페이지 등에서 한국어
title 을 우선 사용하면 정확도가 크게 올라간다.

호출자: sourcing_promote / group_lister.register / Stage 5 fix 등.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def ensure_title_ko(asin: str, force: bool = False) -> Optional[str]:
    """products.title_ko 가 비어있으면 title_en 을 한국어로 번역 후 저장.

    이미 채워져 있으면 그대로 반환 (force=True 면 재번역).
    translation_cache (backend_shared.ai.service) 자동 활용 — 같은 영문 title 은
    한 번만 Gemini 호출.

    반환: 번역 후 (또는 기존) title_ko, 실패 시 None.
    """
    from backend.purchase.database import get_db

    asin = (asin or "").strip().upper()
    if not asin:
        return None

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, title_en, title_ko FROM products WHERE asin=? LIMIT 1",
            (asin,),
        ).fetchone()
    if not row:
        return None
    if row["title_ko"] and not force:
        return row["title_ko"]
    if not row["title_en"]:
        return None

    try:
        from backend_shared.ai.service import translate_text
        result = _run_async(
            translate_text(
                row["title_en"],
                source_lang="en",
                target_lang="ko",
                context="Amazon 상품 제목 — 한국 쇼핑몰(쿠팡/스마트스토어) 검색에 노출될 한국어로",
            )
        )
    except Exception as e:
        logger.warning(f"[title_translator] {asin} translate_text 실패: {e}")
        return row["title_ko"]

    title_ko = (result.get("translated") or "").strip() if result else ""
    if not title_ko:
        return row["title_ko"]

    with get_db() as conn:
        conn.execute(
            "UPDATE products SET title_ko=? WHERE id=?",
            (title_ko, row["id"]),
        )
    logger.info(f"[title_translator] {asin}: title_ko 보강 ({len(title_ko)} chars)")
    return title_ko


def ensure_titles_ko_bulk(asins: list[str], force: bool = False) -> dict:
    """여러 ASIN 의 title_ko 일괄 보강. 결과 통계 dict 반환."""
    cached = translated = failed = 0
    for asin in asins:
        before = _peek_title_ko(asin)
        result = ensure_title_ko(asin, force=force)
        if not result:
            failed += 1
        elif before == result:
            cached += 1
        else:
            translated += 1
    return {
        "total": len(asins),
        "cached": cached,
        "translated": translated,
        "failed": failed,
    }


def _peek_title_ko(asin: str) -> Optional[str]:
    from backend.purchase.database import get_db
    with get_db() as conn:
        r = conn.execute(
            "SELECT title_ko FROM products WHERE asin=? LIMIT 1", (asin,),
        ).fetchone()
    return r["title_ko"] if r else None


def _run_async(coro):
    """async 함수를 sync 컨텍스트에서 안전하게 실행.
    이미 실행 중인 event loop 안이면 새 loop 생성, 아니면 asyncio.run.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        return asyncio.run(coro)
    new_loop = asyncio.new_event_loop()
    try:
        return new_loop.run_until_complete(coro)
    finally:
        new_loop.close()
