# -*- coding: utf-8 -*-
"""리콜 차단 게이트 (2026-07-08) — 정확한 리콜품만.
recall_blocklist 테이블(ASIN) + 리콜 모델번호(Cuisinart 와이어 그릴브러시) 검사.
list_product/그룹등록/마이그 후보에서 사용. 새 리콜은 recall_blocklist에 ASIN 추가만 하면 됨."""
import re
import logging

logger = logging.getLogger(__name__)

# CPSC Conair 리콜 대상 Cuisinart 금속강모 그릴브러시/세트 모델번호 (상품명에 있으면 차단)
_RECALL_MODELS = [
    "CCB-100", "CCB-4125", "CCB-5014", "CCB-6450", "CCB-8012",
    "CCB-4114", "CCB-W2", "CSBS-777",
    "CGS-2010", "CGS-W13", "CGS-5014", "CGS-5020",
]
_MODEL_RE = re.compile("|".join(re.escape(m) for m in _RECALL_MODELS), re.IGNORECASE)

_asin_cache = None


def _load_asins():
    global _asin_cache
    if _asin_cache is not None:
        return _asin_cache
    try:
        from backend.purchase.database import get_db
        with get_db() as conn:
            rows = conn.execute("SELECT asin, reason FROM recall_blocklist").fetchall()
        _asin_cache = {str(r["asin"]): (r["reason"] or "리콜") for r in rows}
    except Exception as e:
        logger.warning(f"[recall] blocklist 로드 실패: {e}")
        _asin_cache = {}
    return _asin_cache


def is_recalled(asin=None, title=None):
    """리콜 대상이면 사유(str), 아니면 None. asin 우선, 없으면 상품명 모델번호 매칭."""
    amap = _load_asins()
    if asin and str(asin) in amap:
        return amap[str(asin)]
    if title and _MODEL_RE.search(str(title)):
        return "Cuisinart 리콜 모델(금속강모 그릴브러시)"
    return None
