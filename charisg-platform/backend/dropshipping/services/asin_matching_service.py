"""
asin_matching_service.py — CJ 상품 → Amazon ASIN 매칭 서비스.

최적화 v3:
- 키워드 추출: 핵심 명사만 추출, 색상/수량/수식어 제거
- 유사도: CJ 토큰 기준 recall (Amazon이 CJ 키워드를 얼마나 포함하는지)
- 다중 검색: 전체 키워드 → 핵심 3단어 fallback
- 가격: amazon_search_agg 테이블에서 키워드별 중위가 활용
- 속도: CatalogItems 클라이언트 싱글턴, DB 배치 쓰기
- ★ v3: 브랜드/카테고리 사전 필터 — 등록 불가 ASIN 사전 제거
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
    get_marketplace_for,
)
from backend.dropshipping.services.marketplace_config import get_config
from backend_shared.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# CatalogItems: 2 req/sec = 120 RPM → 110 안전선
_catalog_limiter = RateLimiter(max_per_minute=110, name="catalog_items")

# 매칭 임계값
MATCH_THRESHOLD_STRONG = 0.55
MATCH_THRESHOLD_MODERATE = 0.35

# ── Amazon 등록 제한 필터 (v3) ─────────────────────
# 브랜드 등록된 ASIN은 brand owner만 리스팅 가능 → 이 브랜드가 아닌 것만 허용
_SAFE_BRANDS = {"", "generic", "unbranded", "no brand", "nobrand"}

# Amazon 승인 필요 카테고리 — 정확한 구문 매칭 (단어 단독 사용 시 오탐 방지)
_RESTRICTED_CATEGORY_PHRASES = [
    # 살충제/농약 (EPA 등록 + 미국 거주 필요)
    "pesticide", "insecticide", "bug killer", "pest control",
    "mosquito killer", "roach killer", "rat poison", "insect killer",
    "pest repell",  # pest repellent / pest repeller
    # 의약품/건강 (FDA)
    "supplement", "medication", "pharmaceutical",
    # 화장품/피부 (FDA)
    "anti wrinkle", "collagen mask", "face serum",
    "skincare set", "skin care set",
    # 의료기기
    "tens unit", "muscle stimulator", "medical device",
    "respirator mask",
    # 무기/위험물
    "torch lighter", "propane torch", "cigar torch",
    "welding gun",  # 용접건 (barbecue lighter 류)
    # SD카드/메모리 (위조품 이슈)
    "memory card", "micro sd card", "sd card",
    # 드론 (FAA)
    "drone", "quadcopter",
    # 스마트링/웨어러블 (인증)
    "smart ring health",
    # 에너지 (FDA)
    "energy strip", "caffeine strip",
]


def _is_brand_restricted(brand: str) -> bool:
    """브랜드 등록된 ASIN인지 확인. True면 등록 불가."""
    return brand.strip().lower() not in _SAFE_BRANDS


def _is_category_restricted(title: str) -> bool:
    """제한 카테고리 상품인지 타이틀 구문으로 확인."""
    title_lower = title.lower()
    return any(phrase in title_lower for phrase in _RESTRICTED_CATEGORY_PHRASES)


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


# ── CatalogItems 클라이언트 (마켓별 캐시) ────────────


_catalog_clients: dict[str, CatalogItems] = {}


def _get_catalog_client(market: str = "US") -> CatalogItems:
    """마켓별 CatalogItems 클라이언트. NA 통합 계정이므로 크레덴셜은 동일."""
    if market not in _catalog_clients:
        cfg = get_config(market)
        mp = get_marketplace_for(cfg["marketplace_id"])
        _catalog_clients[market] = CatalogItems(
            credentials=get_credentials(), marketplace=mp,
        )
    return _catalog_clients[market]


def _clear_catalog_client(market: str = "US"):
    """클라이언트 캐시 초기화 (토큰 만료 등)."""
    _catalog_clients.pop(market, None)


def _get_restricted_phrases(market: str = "US") -> list[str]:
    """마켓별 제한 카테고리 구문 로드."""
    cfg = get_config(market)
    return cfg.get("restricted_phrases", _RESTRICTED_CATEGORY_PHRASES)


def _search_catalog(keywords: str, market: str = "US", page_size: int = 10) -> list[dict]:
    """CatalogItems API 검색 (마켓별)."""
    _catalog_limiter.wait()
    cfg = get_config(market)
    marketplace_id = cfg["marketplace_id"]
    client = _get_catalog_client(market)
    restricted = _get_restricted_phrases(market)

    try:
        resp = client.search_catalog_items(
            keywords=keywords,
            marketplaceIds=[marketplace_id],
            includedData="summaries",
            pageSize=page_size,
        )
    except Exception as e:
        logger.error(f"CatalogItems [{market}] 검색 실패: {e}")
        _clear_catalog_client(market)
        return []

    items = resp.payload.get("items", [])
    results = []
    filtered_brand = 0
    filtered_cat = 0
    for item in items:
        asin = item.get("asin", "")
        summaries = item.get("summaries", [])
        if not summaries:
            continue
        s = summaries[0]
        brand = s.get("brand", "")
        title = s.get("itemName", "")

        # v3: 브랜드/카테고리 사전 필터 (마켓별 제한 목록)
        if _is_brand_restricted(brand):
            filtered_brand += 1
            continue
        title_lower = title.lower()
        if any(phrase in title_lower for phrase in restricted):
            filtered_cat += 1
            continue

        results.append({
            "asin": asin,
            "title": title,
            "brand": brand,
        })

    logger.info(
        f"CatalogItems [{market}] '{keywords[:50]}' → {len(results)}건 "
        f"(브랜드 제외 {filtered_brand}, 카테고리 제외 {filtered_cat})"
    )
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


def _multi_search(product_name: str, market: str = "US") -> list[dict]:
    """다중 키워드 전략으로 검색, 중복 제거.

    1차: 핵심 6단어 검색
    2차: 핵심 3단어 검색 (1차 결과 부족 시)
    """
    kw_full = _extract_keywords(product_name)
    if not kw_full:
        return []

    results = _search_catalog(kw_full, market=market)

    # 1차 결과가 3건 미만이면 짧은 키워드로 재검색
    if len(results) < 3:
        kw_short = _extract_keywords_short(product_name)
        if kw_short and kw_short != kw_full:
            extra = _search_catalog(kw_short, market=market)
            seen = {r["asin"] for r in results}
            for e in extra:
                if e["asin"] not in seen:
                    results.append(e)
                    seen.add(e["asin"])

    return results


# ── 단일 상품 매칭 ───────────────────────────────────


def search_asin_candidates(product_id: int, market: str = "US") -> list[dict]:
    """product_id → CatalogItems 검색 → 후보 목록 반환 + DB 저장 (마켓별)."""
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
    restricted = _get_restricted_phrases(market)

    # v3: CJ 상품 자체가 제한 카테고리면 매칭 스킵 (마켓별 제한 목록)
    pname_lower = product["product_name"].lower()
    if any(phrase in pname_lower for phrase in restricted):
        logger.info(f"[{market}] 상품 {product_id} 제한 카테고리 스킵: {product['product_name'][:50]}")
        return []

    # Amazon 중위가 조회 (search_keyword 또는 category 기반)
    agg_price = _get_agg_price(product.get("search_keyword") or "")
    if not agg_price:
        agg_price = _get_agg_price(product.get("category") or "")

    candidates_raw = _multi_search(product["product_name"], market=market)
    if not candidates_raw:
        return []

    scored = [_score_candidate(product, c, agg_price) for c in candidates_raw]
    scored.sort(key=lambda x: -x["match_score"])

    # DB 배치 저장 (마켓별 분리)
    with get_db() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO asin_match_candidates
               (product_id, asin, amazon_title, amazon_brand, amazon_price,
                title_sim, price_compat, match_score, match_verdict, selected,
                marketplace)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            [(product_id, s["asin"], s["amazon_title"], s["amazon_brand"],
              s["amazon_price"], s["title_sim"], s["price_compat"],
              s["match_score"], s["match_verdict"], market) for s in scored],
        )

    return scored


def find_best_match(product_id: int, market: str = "US") -> Optional[dict]:
    """상품 → ASIN 검색 → 최적 매칭 반환 + matched_asin 업데이트 (마켓별)."""
    candidates = search_asin_candidates(product_id, market=market)
    if not candidates:
        return None

    best = candidates[0]
    if best["match_verdict"] == "reject":
        return None

    with get_db() as conn:
        conn.execute(
            "UPDATE asin_match_candidates SET selected = 1 "
            "WHERE product_id = ? AND asin = ? AND marketplace = ?",
            (product_id, best["asin"], market),
        )
        # matched_asin은 US 매칭 시에만 기본값으로 업데이트 (공유 카탈로그)
        if market == "US":
            conn.execute(
                "UPDATE collected_products SET matched_asin = ? WHERE id = ?",
                (best["asin"], product_id),
            )

    logger.info(
        f"[{market}] 상품 {product_id} → {best['asin']} "
        f"({best['match_verdict']}, score={best['match_score']:.3f})"
    )
    return best


def select_asin(product_id: int, asin: str, market: str = "US") -> dict:
    """수동으로 ASIN 선택/변경 (마켓별)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE asin_match_candidates SET selected = 0 "
            "WHERE product_id = ? AND marketplace = ?",
            (product_id, market),
        )
        conn.execute(
            "UPDATE asin_match_candidates SET selected = 1 "
            "WHERE product_id = ? AND asin = ? AND marketplace = ?",
            (product_id, asin, market),
        )
        if market == "US":
            conn.execute(
                "UPDATE collected_products SET matched_asin = ? WHERE id = ?",
                (asin, product_id),
            )
    return {"product_id": product_id, "asin": asin, "market": market, "status": "selected"}


# ── 일괄 매칭 ────────────────────────────────────────


def batch_match(
    limit: int = 50,
    min_sort_score: float = 0.0,
    market: str = "US",
    progress_cb=None,
) -> dict:
    """미매칭 상품 일괄 ASIN 매칭 (마켓별).

    collected_products는 공유 카탈로그이므로 hard_filter_pass 기준으로 후보 선정.
    마켓별로 별도 ASIN 매칭 결과가 asin_match_candidates에 저장됨.
    """
    with get_db() as conn:
        # 해당 마켓에서 아직 매칭 안 된 상품 조회
        rows = conn.execute(
            """SELECT cp.id, cp.product_name, cp.source_price, cp.calculated_price,
                      cp.amazon_category, cp.category, cp.sort_score
               FROM collected_products cp
               WHERE cp.hard_filter_pass = 1
                 AND NOT EXISTS (
                     SELECT 1 FROM asin_match_candidates amc
                     WHERE amc.product_id = cp.id AND amc.marketplace = ?
                 )
                 AND (cp.sort_score >= ? OR cp.sort_score IS NULL)
               ORDER BY cp.sort_score DESC
               LIMIT ?""",
            (market, min_sort_score, limit),
        ).fetchall()

    total = len(rows)
    matched = 0
    failed = 0
    results = []

    for i, row in enumerate(rows):
        pid = row["id"]
        if progress_cb:
            progress_cb("match", i + 1, total, f"[{market}] 매칭 중: {row['product_name'][:40]}")

        try:
            best = find_best_match(pid, market=market)
            if best:
                matched += 1
                results.append({"product_id": pid, **best})
            else:
                results.append({"product_id": pid, "asin": None, "match_verdict": "no_match"})
        except Exception as e:
            failed += 1
            logger.error(f"[{market}] 상품 {pid} 매칭 실패: {e}")
            results.append({"product_id": pid, "error": str(e)})

    return {
        "processed": total,
        "matched": matched,
        "no_match": total - matched - failed,
        "failed": failed,
        "market": market,
        "results": results,
    }


def get_candidates(product_id: int, market: str = "US") -> list[dict]:
    """상품의 ASIN 매칭 후보 목록 조회 (마켓별)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT asin, amazon_title, amazon_brand, amazon_price,
                      title_sim, price_compat, match_score, match_verdict,
                      selected, searched_at
               FROM asin_match_candidates
               WHERE product_id = ? AND marketplace = ?
               ORDER BY match_score DESC""",
            (product_id, market),
        ).fetchall()
    return [dict(r) for r in rows]


def get_pipeline_summary(market: str = "US") -> dict:
    """ASIN 매칭 파이프라인 현황 (마켓별)."""
    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM collected_products WHERE hard_filter_pass = 1"
        ).fetchone()[0]
        # 해당 마켓에서 매칭된 상품 수
        matched = conn.execute(
            """SELECT COUNT(DISTINCT product_id) FROM asin_match_candidates
               WHERE marketplace = ? AND selected = 1""",
            (market,),
        ).fetchone()[0]
        listed = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE marketplace = ? AND asin IS NOT NULL AND asin != ''",
            (market,),
        ).fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE marketplace = ? AND status = 'active'",
            (market,),
        ).fetchone()[0]
        # 매칭 품질 분포
        verdicts = conn.execute(
            """SELECT match_verdict, COUNT(*) FROM asin_match_candidates
               WHERE selected = 1 AND marketplace = ? GROUP BY match_verdict""",
            (market,),
        ).fetchall()

    return {
        "total_filtered": total,
        "matched": matched,
        "unmatched": total - matched,
        "listed": listed,
        "active": active,
        "market": market,
        "match_quality": {r[0]: r[1] for r in verdicts},
    }
