"""
amazon_keyword_crawler.py — DS 전용 Amazon 키워드 크롤러 (Phase 1+ Gap Score 데이터 수집).

목적:
  GO 후보 상품의 키워드로 Amazon 검색 페이지(/s?k=) 를 크롤링해
  amazon_search_results (개별 리스팅) + amazon_search_agg (집계) 테이블을 채운다.
  scoring_service.calculate_gap_score 가 이 집계 데이터를 소비한다.

네트워크 경로 (2026-04-22 변경):
  기존: Webshare residential 프록시 + requests 직접
  현재: ZenRows API (https://api.zenrows.com/v1/) 경유 + premium_proxy=true
  변경 사유: Webshare pool 이 Amazon WAF 에 차단됨 (datacenter/residential 무관 503/202).

크레딧 / 비용:
  premium_proxy=true, js_render 미사용 기준 요청당 약 5 크레딧.
  검색 결과 HTML 은 JS 렌더 불필요 (SSR 포함됨).

CAPTCHA / 차단:
  ZenRows 가 자동 재시도 + 프록시 로테이션 수행. 클라이언트 측 딜레이는 최소화
  (내부 fallback delayer: 5-10s 간격). CAPTCHA/"Sorry" 마커 감지 시 delayer 실패 기록.

EC2 의존: ZENROWS_API_KEY 환경변수만 필요. 프록시 없음.
"""
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import quote_plus

import requests

from backend_shared.utils.rate_limiter import CrawlerDelayer
from backend.dropshipping.database import get_db

logger = logging.getLogger(__name__)


ZENROWS_API_KEY = os.getenv("ZENROWS_API_KEY", "")
ZENROWS_ENDPOINT = "https://api.zenrows.com/v1/"


def _zenrows_get(amazon_url: str, timeout: int = 60) -> requests.Response:
    """Amazon URL을 ZenRows API 경유로 페치.

    요청당 ~5 크레딧(premium_proxy=true, js_render 미사용).
    ZenRows가 프록시 로테이션/재시도/WAF 우회를 자동 수행.
    """
    if not ZENROWS_API_KEY:
        raise RuntimeError("ZENROWS_API_KEY not configured in environment")

    params = {
        "apikey": ZENROWS_API_KEY,
        "url": amazon_url,
        "premium_proxy": "true",
    }
    return requests.get(ZENROWS_ENDPOINT, params=params, timeout=timeout)


def _detect_blocked(html: str) -> bool:
    """Amazon 차단/에러 페이지 감지.

    ZenRows 를 거쳐도 Amazon 이 차단 페이지를 반환하면 본문에 이 마커들이 포함됨.
    """
    lower = html.lower()
    return (
        "captcha" in lower
        or "Enter the characters you see below" in html
        or "Robot Check" in html
        or "Sorry! Something went wrong" in html
        or "challenge-container" in html
        or "AwsWafIntegration" in html
    )


def _parse_search_page(html: str) -> list[dict]:
    """검색 결과 페이지 HTML → 개별 리스팅 dict 리스트.

    DOM 의존 (data-component-type="s-search-result").
    실패 시 빈 리스트.
    """
    items = []
    blocks = re.findall(
        r'data-asin="([A-Z0-9]{10})"[^>]*>(.*?)</div></div></div>',
        html,
        re.DOTALL,
    )
    for asin, block in blocks:
        title_m = re.search(r'aria-label="([^"]+)"', block) or re.search(r'<span class="[^"]*a-text-normal[^"]*">([^<]+)</span>', block)
        price_m = re.search(r'<span class="a-offscreen">\$([\d,.]+)</span>', block)
        review_m = re.search(r'(\d+(?:,\d+)*)\s*(?:rating|review)', block, re.I)
        fbm_m = re.search(r'Ships from\s+([^<]+)<', block)

        items.append({
            "asin": asin,
            "title": (title_m.group(1) if title_m else "").strip(),
            "price": float(price_m.group(1).replace(",", "")) if price_m else None,
            "review_count": int(review_m.group(1).replace(",", "")) if review_m else 0,
            "is_fbm": "fbm" in (fbm_m.group(1).lower() if fbm_m else ""),
        })
    return items


def _aggregate(items: list[dict]) -> dict:
    """개별 리스팅 → 키워드 집계."""
    prices = sorted([i["price"] for i in items if i.get("price")])
    if not prices:
        return {
            "price_min": None, "price_p25": None, "price_median": None,
            "price_p75": None, "price_max": None,
            "avg_review_count": 0, "min_review_count": 0,
            "fbm_count": 0, "total_results": len(items),
        }

    def _pct(arr, p):
        idx = int(len(arr) * p)
        return arr[min(idx, len(arr) - 1)]

    reviews = [i["review_count"] for i in items if i.get("review_count") is not None]
    return {
        "price_min": prices[0],
        "price_p25": _pct(prices, 0.25),
        "price_median": _pct(prices, 0.50),
        "price_p75": _pct(prices, 0.75),
        "price_max": prices[-1],
        "avg_review_count": int(sum(reviews) / len(reviews)) if reviews else 0,
        "min_review_count": min(reviews) if reviews else 0,
        "fbm_count": sum(1 for i in items if i.get("is_fbm")),
        "total_results": len(items),
    }


def crawl_keywords(
    keywords: list[str],
    delayer: Optional[CrawlerDelayer] = None,
) -> dict:
    """키워드 리스트를 순차 크롤링 → DB 저장.

    네트워크: ZenRows API 경유. 프록시 불필요.
    Returns: {"crawled": N, "aborted": bool, "captcha_count": N, "total_keywords": N}
    """
    if delayer is None:
        # ZenRows 가 rate-limit 관리 → 클라이언트 측 딜레이 축소 (15-25s → 5-10s)
        delayer = CrawlerDelayer(
            delay_min=5.0,
            delay_max=10.0,
            name="amazon_keyword_crawler_zr",
        )

    captcha_count = 0
    crawled = 0

    for kw in keywords:
        if not delayer.before_request():
            logger.error("⛔ [ZenRows] 크롤러 중단 — 연속 실패 임계값 초과")
            break

        url = f"https://www.amazon.com/s?k={quote_plus(kw)}&ref=nb_sb_noss"
        try:
            r = _zenrows_get(url, timeout=60)

            if r.status_code != 200:
                logger.warning(f"[ZenRows][{kw}] HTTP {r.status_code}")
                delayer.report_failure()
                continue

            size_kb = len(r.content) // 1024

            if _detect_blocked(r.text):
                captcha_count += 1
                logger.warning(f"[ZenRows][{kw}] 차단/CAPTCHA 마커 감지 (size={size_kb}KB)")
                delayer.report_failure(captcha=True)
                continue

            items = _parse_search_page(r.text)
            if not items:
                logger.warning(f"[ZenRows][{kw}] 파싱 결과 0건 (size={size_kb}KB)")
                delayer.report_failure()
                continue

            agg = _aggregate(items)
            _save_results(kw, items, agg)
            delayer.report_success()
            crawled += 1
            logger.info(
                f"[ZenRows][{kw}] 수집 {len(items)}개, "
                f"p75=${agg['price_p75']}, size={size_kb}KB"
            )

        except Exception as e:
            logger.error(f"[ZenRows][{kw}] 크롤 실패: {e}")
            delayer.report_failure()

    return {
        "crawled": crawled,
        "aborted": delayer.aborted,
        "captcha_count": captcha_count,
        "total_keywords": len(keywords),
    }


def _save_results(keyword: str, items: list[dict], agg: dict) -> None:
    with get_db() as conn:
        for it in items:
            if not it.get("asin"):
                continue
            conn.execute(
                """INSERT OR REPLACE INTO amazon_search_results
                   (keyword, asin, title, price, review_count, is_fbm, collected_at)
                   VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (keyword, it["asin"], it["title"], it.get("price"),
                 it.get("review_count", 0), 1 if it.get("is_fbm") else 0),
            )

        conn.execute(
            """INSERT OR REPLACE INTO amazon_search_agg
               (keyword, price_min, price_p25, price_median, price_p75, price_max,
                avg_review_count, min_review_count, fbm_count, total_results, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (keyword, agg.get("price_min"), agg.get("price_p25"),
             agg.get("price_median"), agg.get("price_p75"), agg.get("price_max"),
             agg.get("avg_review_count", 0), agg.get("min_review_count", 0),
             agg.get("fbm_count", 0), agg.get("total_results", 0)),
        )


def get_keywords_from_go_products(limit: int = 100) -> list[str]:
    """GO 판정 상품에서 크롤링용 키워드 추출."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT search_keyword FROM collected_products
               WHERE business_model='dropship'
                 AND go_decision IN ('GO', 'GO_ORGANIC')
                 AND search_keyword IS NOT NULL
                 AND search_keyword != ''
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [r["search_keyword"] for r in rows]
