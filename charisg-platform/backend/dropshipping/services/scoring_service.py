"""
scoring_service.py — Phase 0 스코어링 파이프라인 v2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
2축 곱셈 모델: Final Score = Demand Score × Margin Score

Demand Score (0~1):
  = Category Demand × 0.5 + Trends Score × 0.5
  - Category Demand: Amazon 카테고리별 수동 매핑 (0.2 / 0.5 / 0.8)
  - Trends Score: Google Trends pytrends 연동, 실패 시 0.4 fallback

Margin Score (0~1):
  = (real_margin_pct / 60) × Price Factor
  - Price Factor: 가격대별 보정계수 ($20~45 → 1.0 등)

등급:
  Demand: A ≥ 0.65, B ≥ 0.40, C < 0.40
  Margin: A ≥ 0.50, B ≥ 0.25, C < 0.25
  Matrix Group: AA, AB, AC, BA, BB, BC, CA, CB, CC

sort_score = round(demand_score × margin_score, 3)
"""
import logging
from typing import Optional

from backend.dropshipping.database import get_db
from backend.dropshipping.services.amazon_fee_service import get_amazon_category

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Amazon 카테고리별 Category Demand (수동 매핑, 방법 b)
# 0.8 = 수요 높음 / 0.5 = 보통 / 0.2 = 낮음
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CATEGORY_DEMAND: dict[str, float] = {
    "Home & Kitchen":    0.8,   # 홈데코: Etsy/Amazon 공통 강세
    "Garden & Outdoor":  0.5,   # 시즌성
    "Furniture":         0.5,   # 무거워서 드랍쉬핑 불리 (Hard Filter에서 걸러지겠지만)
    "Pet Supplies":      0.8,   # 반복 구매 높음
    "Toys & Games":      0.5,   # 시즌성 (Q4 강세)
    "Sports & Outdoors": 0.5,
    "Clothing":          0.5,   # 사이즈 리스크
    "Shoes":             0.2,   # 사이즈 + 반품률 높음
    "Jewelry":           0.2,   # 수수료 20% + 진품 의심
    "Watches":           0.2,   # 수수료 16% + 진품 의심
    "Electronics":       0.2,   # 수수료 낮지만 경쟁 극심
    "Electronics Acc.":  0.5,   # 경쟁 치열하지만 수요도 높음
    "Automotive":        0.5,
    "Beauty":            0.8,   # 반복 구매 + 마진 좋음
    "Baby Products":     0.5,
    "Health & Household": 0.5,
    "Tools & Home":      0.5,
    "Office Products":   0.2,
    "Everything Else":   0.5,
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Price Factor (판매가 기준 보정계수)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _price_factor(sale_price: float) -> float:
    """가격대별 Margin Score 보정계수
    $20~$45: 1.0 (최적 — 충동구매 + 적정 마진)
    $15~$19: 0.85 (저가 — 마진 박하지만 전환율 높음)
    $46~$70: 0.90 (고가 — 마진 좋지만 전환율 낮음)
    그 외: Hard Filter에서 이미 걸러짐
    """
    if 20 <= sale_price <= 45:
        return 1.0
    elif 15 <= sale_price < 20:
        return 0.85
    elif 45 < sale_price <= 70:
        return 0.90
    return 0.75  # 예외 (Hard Filter 통과했지만 범위 밖)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 등급 산출
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _demand_grade(score: float) -> str:
    if score >= 0.65: return "A"
    if score >= 0.40: return "B"
    return "C"


def _margin_grade(score: float) -> str:
    if score >= 0.50: return "A"
    if score >= 0.25: return "B"
    return "C"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Google Trends 연동
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TRENDS_FALLBACK = 0.4  # pytrends 실패 시 중립값


def _get_trend_score(keyword: str) -> float:
    """Google Trends에서 키워드 관심도 조회 (0~1)

    pytrends 실패 시 0.4 fallback.
    """
    try:
        from backend.dropshipping.services.google_trends_service import get_interest_over_time
        result = get_interest_over_time([keyword], timeframe="now 7-d")
        if result and keyword in result:
            # pytrends 반환값: 0~100 → 0~1 정규화
            values = result[keyword]
            if values:
                avg_interest = sum(values) / len(values)
                return round(avg_interest / 100, 2)
    except Exception as e:
        logger.debug(f"Google Trends 조회 실패 ({keyword}): {e}")

    return TRENDS_FALLBACK


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 스코어 계산
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def calculate_demand_score(
    amazon_category: str,
    trend_score: float,
) -> float:
    """Demand Score = Category Demand × 0.5 + Trends Score × 0.5"""
    cat_demand = CATEGORY_DEMAND.get(amazon_category, 0.5)
    return round(cat_demand * 0.5 + trend_score * 0.5, 3)


def calculate_margin_score(
    real_margin_pct: float,
    sale_price: float,
) -> float:
    """Margin Score = (real_margin_pct / 60) × Price Factor

    25% 미만은 Hard Filter에서 제거됨. 60% 이상 = raw 1.0.
    """
    if real_margin_pct <= 0:
        return 0
    raw = min(real_margin_pct / 60, 1.0)
    pf = _price_factor(sale_price)
    return round(raw * pf, 3)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Gap Score (Phase 1+, amazon_search_agg 기반)
#   = review_gap × 0.45 + price_position × 0.35 + fbm_ratio × 0.20
# 데이터 없으면 1.0 (sort_score 영향 없음)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def calculate_gap_score(product: dict) -> float:
    """3축 중 G(Gap) 점수.

    필요 컬럼 (collected_products 또는 join):
        - keyword_review_min: 해당 키워드 최저 리뷰 수
        - amazon_price_p75:   p75 가격
        - amazon_price_max:   최대 가격
        - keyword_fbm_ratio:  FBM 비율 (0~1)

    데이터 없으면 1.0 반환 (sort_score 무영향).
    """
    keyword_min_reviews = product.get("keyword_min_reviews")
    amazon_p75 = product.get("amazon_price_p75")
    amazon_max = product.get("amazon_price_max")
    fbm_ratio = product.get("keyword_fbm_ratio")
    our_price = product.get("calculated_price") or 0

    if keyword_min_reviews is None and amazon_p75 is None and fbm_ratio is None:
        return 1.0

    # review_gap: 최저 리뷰가 적을수록 진입 쉬움 (0~1)
    if keyword_min_reviews is not None:
        if keyword_min_reviews <= 50:
            review_gap = 1.0
        elif keyword_min_reviews <= 200:
            review_gap = 0.7
        elif keyword_min_reviews <= 1000:
            review_gap = 0.4
        else:
            review_gap = 0.15
    else:
        review_gap = 0.5

    # price_position: 우리 가격이 p75 이하면 1.0, max 이하면 0.5, 초과면 0
    if amazon_p75 is not None and our_price > 0:
        if our_price <= amazon_p75:
            price_pos = 1.0
        elif amazon_max is not None and our_price <= amazon_max:
            price_pos = 0.5
        else:
            price_pos = 0.0
    else:
        price_pos = 0.5

    # fbm_ratio: FBM 비율이 높을수록 우리(FBM) 진입 유리
    if fbm_ratio is not None:
        fbm = max(0.0, min(1.0, fbm_ratio))
    else:
        fbm = 0.5

    score = review_gap * 0.45 + price_pos * 0.35 + fbm * 0.20
    return round(score, 3)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 메인 파이프라인
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_hard_filter_products() -> list[dict]:
    """Step 1: Hard Filter 통과 상품 추출"""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, external_id, product_name, category, source_price,
                   calculated_price, margin_pct, shipping_cost, stock_quantity,
                   weight_g, image_count, image_url, url
            FROM collected_products
            WHERE source = 'cj'
              AND business_model = 'dropship'
              AND hard_filter_pass = 1
            ORDER BY margin_pct DESC
        """).fetchall()

    return [dict(r) for r in rows]


def run_scoring_pipeline(
    use_trends: bool = True,
    collect_cj: bool = True,
    progress_cb=None,
) -> list[dict]:
    """Step 0~4 통합 파이프라인 실행 (스펙 기준: 수집 → 필터 → 스코어)

    Args:
        use_trends: Google Trends 조회 여부 (False면 fallback 0.4 사용)
        collect_cj: CJ 전수 수집 실행 여부 (True면 먼저 수집)
        progress_cb: callable(phase, current, total, message) — 진행률 콜백

    Returns:
        스코어링 완료된 상품 리스트 (sort_score 내림차순)
    """
    # Step 0: CJ 전수 수집 (스펙: CJ 38K → Collected 6.2K → Hard Filter 335)
    if collect_cj:
        from backend.dropshipping.services import cj_service
        logger.info("Step 0: CJ 카탈로그 전수 수집 시작")
        if progress_cb:
            progress_cb("collect", 0, 1, "CJ 카탈로그 수집 시작")
        stats = cj_service.collect_full_catalog(progress_cb=progress_cb)
        logger.info(f"Step 0: CJ 수집 완료 — {stats}")

    # Step 1: Hard Filter 통과 상품
    if progress_cb:
        progress_cb("score", 0, 1, "Hard Filter 통과 상품 조회")
    products = get_hard_filter_products()
    if not products:
        logger.warning("Hard Filter 통과 상품 없음")
        return []
    logger.info(f"Step 1: Hard Filter 통과 {len(products)}개")

    # Step 2: Amazon 카테고리 매핑
    for p in products:
        p["amazon_category"] = get_amazon_category(
            p.get("category", ""), p.get("product_name", ""),
        )
    logger.info("Step 2: Amazon 카테고리 매핑 완료")

    # Step 3~4: 스코어 산출
    scored = []
    for p in products:
        # Trends Score
        if use_trends:
            keyword = p.get("category") or p.get("product_name", "").split()[0] if p.get("product_name") else ""
            trend = _get_trend_score(keyword)
        else:
            trend = TRENDS_FALLBACK

        # Demand Score
        demand = calculate_demand_score(p["amazon_category"], trend)
        d_grade = _demand_grade(demand)

        # Margin Score
        margin = calculate_margin_score(
            p.get("margin_pct", 0),
            p.get("calculated_price", 0) or 0,
        )
        m_grade = _margin_grade(margin)

        # Gap Score (Phase 1+) — amazon_search_agg 데이터가 있을 때만 의미있는 값
        # 없으면 1.0 (sort_score 영향 없음)
        gap = calculate_gap_score(p)

        # Matrix Group (D × M, 2축)
        matrix = d_grade + m_grade
        # Sort Score (D × G × M, 3축 곱셈 정렬)
        sort_score = round(demand * gap * margin, 3)

        p.update({
            "trend_score": trend,
            "demand_score": demand,
            "demand_grade": d_grade,
            "gap_score": gap,
            "margin_score": margin,
            "margin_grade": m_grade,
            "matrix_group": matrix,
            "sort_score": sort_score,
        })
        scored.append(p)

    # sort_score 내림차순 정렬
    scored.sort(key=lambda x: x["sort_score"], reverse=True)

    if scored:
        logger.info(f"Step 3~4: 스코어링 완료 — 최고 {scored[0]['sort_score']:.3f}, "
                     f"최저 {scored[-1]['sort_score']:.3f}")

    # DB에 스코어 저장
    _save_scores(scored)

    return scored


def _save_scores(products: list[dict]):
    """스코어 결과를 DB에 저장"""
    with get_db() as conn:
        for p in products:
            conn.execute(
                """UPDATE collected_products
                   SET amazon_category = ?,
                       trend_score = ?,
                       demand_score = ?, demand_grade = ?,
                       gap_score = ?,
                       margin_score = ?, margin_grade = ?,
                       matrix_group = ?, sort_score = ?,
                       score = ?, grade = ?,
                       status = 'candidate',
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (
                    p.get("amazon_category", ""),
                    p["trend_score"],
                    p["demand_score"], p["demand_grade"],
                    p.get("gap_score", 1.0),
                    p["margin_score"], p["margin_grade"],
                    p["matrix_group"], p["sort_score"],
                    round(p["sort_score"] * 100, 1),  # score: 0~100 스케일 (호환)
                    p["matrix_group"],                  # grade: matrix_group으로 대체
                    p["id"],
                ),
            )
    logger.info(f"DB에 {len(products)}개 스코어 저장 완료")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 리포트
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_scoring_report() -> dict:
    """B-1: 스코어링 결과 리포트

    Returns:
        - 총 상품 수 (CJ 전체)
        - Hard Filter 통과/탈락 수
        - 탈락 사유별 비율
        - 등급별 분포 (matrix_group)
        - 상위 10개 상품
    """
    with get_db() as conn:
        # 전체 CJ 드랍쉬핑 상품 수
        total = conn.execute(
            "SELECT COUNT(*) as c FROM collected_products WHERE source='cj' AND business_model='dropship'"
        ).fetchone()["c"]

        # Hard Filter 통과 수
        passed = conn.execute(
            "SELECT COUNT(*) as c FROM collected_products WHERE source='cj' AND business_model='dropship' AND hard_filter_pass=1"
        ).fetchone()["c"]

        # 탈락 사유별 집계
        fail_reasons_raw = conn.execute("""
            SELECT filter_fail_reason, COUNT(*) as c
            FROM collected_products
            WHERE source='cj' AND business_model='dropship' AND hard_filter_pass=0
              AND filter_fail_reason IS NOT NULL
            GROUP BY filter_fail_reason
            ORDER BY c DESC
        """).fetchall()
        fail_reasons = {r["filter_fail_reason"]: r["c"] for r in fail_reasons_raw}

        # Matrix Group 분포
        matrix_raw = conn.execute("""
            SELECT matrix_group, COUNT(*) as c
            FROM collected_products
            WHERE source='cj' AND business_model='dropship' AND hard_filter_pass=1
              AND matrix_group IS NOT NULL
            GROUP BY matrix_group
            ORDER BY c DESC
        """).fetchall()
        matrix_dist = {r["matrix_group"]: r["c"] for r in matrix_raw}

        # 상위 10개
        top10_raw = conn.execute("""
            SELECT id, product_name, category, amazon_category, source_price,
                   calculated_price, margin_pct, demand_score, demand_grade,
                   margin_score, margin_grade, matrix_group, sort_score, url
            FROM collected_products
            WHERE source='cj' AND business_model='dropship' AND hard_filter_pass=1
              AND sort_score IS NOT NULL
            ORDER BY sort_score DESC
            LIMIT 10
        """).fetchall()

        # amazon_category가 DB에 없으면 product_name에서 재매핑
        top10 = []
        for r in top10_raw:
            d = dict(r)
            if not d.get("amazon_category"):
                d["amazon_category"] = get_amazon_category(
                    d.get("category", ""), d.get("product_name", ""))
            top10.append(d)

    failed = total - passed

    return {
        "total_cj_products": total,
        "hard_filter_passed": passed,
        "hard_filter_failed": failed,
        "pass_rate": round(passed / total * 100, 1) if total > 0 else 0,
        "fail_reasons": fail_reasons,
        "matrix_distribution": matrix_dist,
        "top10": top10,
    }
