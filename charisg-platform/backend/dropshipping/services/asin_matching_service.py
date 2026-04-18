"""
asin_matching_service.py — CJ 상품 → Amazon ASIN 매칭 서비스.

최적화 v2:
- 키워드 추출: 핵심 명사만 추출, 색상/수량/수식어 제거
- 유사도: CJ 토큰 기준 recall (Amazon이 CJ 키워드를 얼마나 포함하는지)
- 다중 검색: 전체 키워드 → 핵심 3단어 fallback
- 가격: amazon_search_agg 테이블에서 키워드별 중위가 활용
- 속도: CatalogItems 클라이언트 싱글턴, DB 배치 쓰기
"""
import logging
import re
import time
from functools import lru_cache
from typing import Optional

from sp_api.api import CatalogItems
from sp_api.base import Marketplaces

from backend.dropshipping.database import get_db
from backend.dropshipping.services.amazon_sp_api_service import (
    get_credentials,
    get_marketplace,
)
from backend_shared.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# CatalogItems: 2 req/sec = 120 RPM → 110 안전선
_catalog_limiter = RateLimiter(max_per_minute=110, name="catalog_items")

MARKETPLACE_ID = "ATVPDKIKX0DER"

# 매칭 임계값
MATCH_THRESHOLD_STRONG = 0.55
MATCH_THRESHOLD_MODERATE = 0.35

# ── 노이즈 사전 ─────────────────────────────────────

_NOISE_WORDS = {
    "the", "a", "an", "and", "or", "for", "with", "in", "on", "of", "to",
    "is", "are", "be", "was", "were", "been", "being", "its", "it", "by",
    "at", "from", "up", "out", "if", "about", "into", "through", "during",
}

# 색상 (검색 노이즈, 매칭엔 유용)
_COLORS = {
    "red", "blue", "green", "black", "white", "silver", "gold", "pink",
    "purple", "orange", "yellow", "brown", "grey", "gray", "beige",
    "navy", "teal", "cyan", "magenta", "ivory", "khaki",
}

# 검색에서 제거할 수식어/마케팅 단어
_MODIFIERS = {
    "new", "hot", "sale", "free", "shipping", "wholesale", "retail",
    "high", "quality", "best", "good", "great", "nice", "premium",
    "luxury", "professional", "portable", "mini", "large", "small",
    "upgraded", "improved", "latest", "modern", "classic", "elegant",
    "durable", "lightweight", "heavy", "duty", "ultra", "super",
    "fashion", "stylish", "cute", "creative", "unique", "special",
    "functional", "practical", "universal", "adjustable",
    "comes", "built", "included", "includes", "features",
}

# 수량/단위 (검색에서 제거)
_UNITS = {
    "pcs", "pce", "set", "pack", "lot", "pair", "piece", "pieces",
    "mm", "cm", "inch", "inches", "ft", "oz", "lb", "lbs", "kg",
    "ml", "gallon",
}

_SKIP_TOKENS = _NOISE_WORDS | _MODIFIERS | _UNITS

_RE_NON_ALPHA = re.compile(r"[^a-zA-Z0-9\s]")
_RE_DIGITS_ONLY = re.compile(r"^\d+$")
_RE_SIZE_PATTERN = re.compile(r"\d+\s*(x|×)\s*\d+", re.IGNORECASE)


# ── 키워드 추출 (v2: 핵심 명사 중심) ────────────────


def _extract_keywords(product_name: str, max_tokens: int = 6) -> str:
    """CJ 상품명 → Amazon 검색 키워드.

    색상, 수량, 수식어를 제거하고 핵심 상품 유형 토큰만 남긴다.
    예: "Blue Direct-flow Spray Gun Comes With A Bottle Opener, A Lighter"
      → "direct flow spray gun bottle opener"
    """
    name = _RE_NON_ALPHA.sub(" ", product_name.lower())
    # 크기 패턴 제거 (e.g., "10x15")
    name = _RE_SIZE_PATTERN.sub("", name)

    tokens = name.split()
    core = []
    for t in tokens:
        if len(t) <= 1:
            continue
        if t in _SKIP_TOKENS or t in _COLORS:
            continue
        if _RE_DIGITS_ONLY.match(t):
            continue
        core.append(t)

    return " ".join(core[:max_tokens])


def _extract_keywords_short(product_name: str) -> str:
    """핵심 3단어만 추출 (fallback 검색용)."""
    return _extract_keywords(product_name, max_tokens=3)


# ── 유사도 (v2: recall 기반) ─────────────────────────


def _title_similarity(cj_title: str, amazon_title: str) -> float:
    """CJ 키워드가 Amazon 제목에 얼마나 포함되는지 (recall 기반).

    Jaccard는 Amazon 제목이 길수록 불리해진다.
    대신 CJ 핵심 토큰의 recall을 측정: CJ 토큰 중 Amazon에 있는 비율.
    """
    cj_clean = _RE_NON_ALPHA.sub(" ", cj_title.lower())
    amz_clean = _RE_NON_ALPHA.sub(" ", amazon_title.lower())

    cj_tokens = set(cj_clean.split()) - _SKIP_TOKENS - _COLORS
    amz_tokens = set(amz_clean.split()) - _SKIP_TOKENS

    if not cj_tokens:
        return 0.0

    # CJ 토큰이 Amazon에 포함된 비율 (recall)
    hit = sum(1 for t in cj_tokens if t in amz_tokens)
    recall = hit / len(cj_tokens)

    # 보너스: 색상 매치 (같은 색상이면 +0.1)
    cj_colors = set(cj_clean.split()) & _COLORS
    amz_colors = set(amz_clean.split()) & _COLORS
    color_bonus = 0.1 if cj_colors and cj_colors & amz_colors else 0.0

    return min(recall + color_bonus, 1.0)


def _price_compatibility(
    source_price: float,
    calculated_price: float,
    amazon_price: Optional[float],
) -> float:
    """가격 호환성 (0.0~1.0).

    source_price: CJ 원가, calculated_price: 판매 예정가, amazon_price: 시장가.
    """
    if amazon_price is None or amazon_price <= 0:
        return 0.5  # 가격 정보 없으면 중립

    if source_price <= 0:
        return 0.0

    # 핵심: 우리 원가 대비 Amazon 시장가에서 마진이 나는가
    margin_ratio = (amazon_price - source_price) / amazon_price if amazon_price > 0 else 0

    if margin_ratio >= 0.5:
        return 1.0   # 마진 50%+: 최적
    if margin_ratio >= 0.3:
        return 0.85  # 마진 30%+: 양호
    if margin_ratio >= 0.15:
        return 0.6   # 마진 15%+: 가능
    if margin_ratio >= 0:
        return 0.3   # 마진 거의 없음
    return 0.0       # 역마진


# ── CatalogItems 클라이언트 (싱글턴) ─────────────────


@lru_cache(maxsize=1)
def _get_catalog_client() -> CatalogItems:
    """CatalogItems 클라이언트 싱글턴."""
    return CatalogItems(credentials=get_credentials(), marketplace=get_marketplace())


def _search_catalog(keywords: str, page_size: int = 10) -> list[dict]:
    """CatalogItems API 검색."""
    _catalog_limiter.wait()
    client = _get_catalog_client()

    try:
        resp = client.search_catalog_items(
            keywords=keywords,
            marketplaceIds=[MARKETPLACE_ID],
            includedData="summaries",
            pageSize=page_size,
        )
    except Exception as e:
        logger.error(f"CatalogItems 검색 실패: {e}")
        # 클라이언트 캐시 초기화 (토큰 만료 등)
        _get_catalog_client.cache_clear()
        return []

    items = resp.payload.get("items", [])
    results = []
    for item in items:
        asin = item.get("asin", "")
        summaries = item.get("summaries", [])
        if not summaries:
            continue
        s = summaries[0]
        results.append({
            "asin": asin,
            "title": s.get("itemName", ""),
            "brand": s.get("brand", ""),
        })

    logger.info(f"CatalogItems '{keywords[:50]}' → {len(results)}건")
    return results


# ── 가격 조회 (amazon_search_agg 활용) ──────────────


def _get_agg_price(keyword: str) -> Optional[float]:
    """amazon_search_agg에서 키워드별 Amazon 중위가 조회."""
    if not keyword:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT price_median FROM amazon_search_agg WHERE keyword = ?",
            (keyword,),
        ).fetchone()
    return row["price_median"] if row else None


# ── 점수 산출 ────────────────────────────────────────


def _score_candidate(product: dict, candidate: dict, agg_price: Optional[float]) -> dict:
    """CJ-상품 / Amazon-ASIN 후보 점수."""
    title_sim = _title_similarity(
        product.get("product_name", ""),
        candidate.get("title", ""),
    )

    amazon_price = agg_price  # 키워드 기반 중위가
    price_compat = _price_compatibility(
        product.get("source_price", 0),
        product.get("calculated_price", 0),
        amazon_price,
    )

    # 가중치: 제목 유사도 65%, 가격 호환 35%
    match_score = title_sim * 0.65 + price_compat * 0.35

    if match_score >= MATCH_THRESHOLD_STRONG:
        verdict = "strong"
    elif match_score >= MATCH_THRESHOLD_MODERATE:
        verdict = "moderate"
    elif match_score >= 0.2:
        verdict = "weak"
    else:
        verdict = "reject"

    return {
        "asin": candidate["asin"],
        "amazon_title": candidate.get("title", ""),
        "amazon_brand": candidate.get("brand", ""),
        "amazon_price": amazon_price,
        "title_sim": round(title_sim, 4),
        "price_compat": round(price_compat, 4),
        "match_score": round(match_score, 4),
        "match_verdict": verdict,
    }


# ── 다중 검색 전략 ──────────────────────────────────


def _multi_search(product_name: str) -> list[dict]:
    """다중 키워드 전략으로 검색, 중복 제거.

    1차: 핵심 6단어 검색
    2차: 핵심 3단어 검색 (1차 결과 부족 시)
    """
    kw_full = _extract_keywords(product_name)
    if not kw_full:
        return []

    results = _search_catalog(kw_full)

    # 1차 결과가 3건 미만이면 짧은 키워드로 재검색
    if len(results) < 3:
        kw_short = _extract_keywords_short(product_name)
        if kw_short and kw_short != kw_full:
            extra = _search_catalog(kw_short)
            seen = {r["asin"] for r in results}
            for e in extra:
                if e["asin"] not in seen:
                    results.append(e)
                    seen.add(e["asin"])

    return results


# ── 단일 상품 매칭 ───────────────────────────────────


def search_asin_candidates(product_id: int) -> list[dict]:
    """product_id → CatalogItems 검색 → 후보 목록 반환 + DB 저장."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT product_name, source_price, calculated_price,
                      amazon_category, category, search_keyword
               FROM collected_products WHERE id = ?""",
            (product_id,),
        ).fetchone()

    if not row:
        raise ValueError(f"상품 ID {product_id} 없음")

    product = dict(row)

    # Amazon 중위가 조회 (search_keyword 또는 category 기반)
    agg_price = _get_agg_price(product.get("search_keyword") or "")
    if not agg_price:
        agg_price = _get_agg_price(product.get("category") or "")

    candidates_raw = _multi_search(product["product_name"])
    if not candidates_raw:
        return []

    scored = [_score_candidate(product, c, agg_price) for c in candidates_raw]
    scored.sort(key=lambda x: -x["match_score"])

    # DB 배치 저장
    with get_db() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO asin_match_candidates
               (product_id, asin, amazon_title, amazon_brand, amazon_price,
                title_sim, price_compat, match_score, match_verdict, selected)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            [(product_id, s["asin"], s["amazon_title"], s["amazon_brand"],
              s["amazon_price"], s["title_sim"], s["price_compat"],
              s["match_score"], s["match_verdict"]) for s in scored],
        )

    return scored


def find_best_match(product_id: int) -> Optional[dict]:
    """상품 → ASIN 검색 → 최적 매칭 반환 + matched_asin 업데이트."""
    candidates = search_asin_candidates(product_id)
    if not candidates:
        return None

    best = candidates[0]
    if best["match_verdict"] == "reject":
        return None

    with get_db() as conn:
        conn.execute(
            "UPDATE asin_match_candidates SET selected = 1 "
            "WHERE product_id = ? AND asin = ?",
            (product_id, best["asin"]),
        )
        conn.execute(
            "UPDATE collected_products SET matched_asin = ? WHERE id = ?",
            (best["asin"], product_id),
        )

    logger.info(
        f"상품 {product_id} → {best['asin']} "
        f"({best['match_verdict']}, score={best['match_score']:.3f})"
    )
    return best


def select_asin(product_id: int, asin: str) -> dict:
    """수동으로 ASIN 선택/변경."""
    with get_db() as conn:
        conn.execute(
            "UPDATE asin_match_candidates SET selected = 0 WHERE product_id = ?",
            (product_id,),
        )
        conn.execute(
            "UPDATE asin_match_candidates SET selected = 1 "
            "WHERE product_id = ? AND asin = ?",
            (product_id, asin),
        )
        conn.execute(
            "UPDATE collected_products SET matched_asin = ? WHERE id = ?",
            (asin, product_id),
        )
    return {"product_id": product_id, "asin": asin, "status": "selected"}


# ── 일괄 매칭 ────────────────────────────────────────


def batch_match(
    limit: int = 50,
    min_sort_score: float = 0.0,
    progress_cb=None,
) -> dict:
    """미매칭 상품 일괄 ASIN 매칭."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, product_name, source_price, calculated_price,
                      amazon_category, category, sort_score
               FROM collected_products
               WHERE hard_filter_pass = 1
                 AND (matched_asin IS NULL OR matched_asin = '')
                 AND (sort_score >= ? OR sort_score IS NULL)
               ORDER BY sort_score DESC
               LIMIT ?""",
            (min_sort_score, limit),
        ).fetchall()

    total = len(rows)
    matched = 0
    failed = 0
    results = []

    for i, row in enumerate(rows):
        pid = row["id"]
        if progress_cb:
            progress_cb("match", i + 1, total, f"매칭 중: {row['product_name'][:40]}")

        try:
            best = find_best_match(pid)
            if best:
                matched += 1
                results.append({"product_id": pid, **best})
            else:
                results.append({"product_id": pid, "asin": None, "match_verdict": "no_match"})
        except Exception as e:
            failed += 1
            logger.error(f"상품 {pid} 매칭 실패: {e}")
            results.append({"product_id": pid, "error": str(e)})

    return {
        "processed": total,
        "matched": matched,
        "no_match": total - matched - failed,
        "failed": failed,
        "results": results,
    }


def get_candidates(product_id: int) -> list[dict]:
    """상품의 ASIN 매칭 후보 목록 조회."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT asin, amazon_title, amazon_brand, amazon_price,
                      title_sim, price_compat, match_score, match_verdict,
                      selected, searched_at
               FROM asin_match_candidates
               WHERE product_id = ?
               ORDER BY match_score DESC""",
            (product_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_pipeline_summary() -> dict:
    """ASIN 매칭 파이프라인 현황."""
    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM collected_products WHERE hard_filter_pass = 1"
        ).fetchone()[0]
        matched = conn.execute(
            "SELECT COUNT(*) FROM collected_products "
            "WHERE hard_filter_pass = 1 AND matched_asin IS NOT NULL AND matched_asin != ''"
        ).fetchone()[0]
        listed = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE asin IS NOT NULL AND asin != ''"
        ).fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE status = 'active'"
        ).fetchone()[0]
        # 매칭 품질 분포
        verdicts = conn.execute(
            """SELECT match_verdict, COUNT(*) FROM asin_match_candidates
               WHERE selected = 1 GROUP BY match_verdict"""
        ).fetchall()

    return {
        "total_filtered": total,
        "matched": matched,
        "unmatched": total - matched,
        "listed": listed,
        "active": active,
        "match_quality": {r[0]: r[1] for r in verdicts},
    }
