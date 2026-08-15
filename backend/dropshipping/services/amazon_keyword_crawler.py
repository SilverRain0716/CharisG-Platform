"""
amazon_keyword_crawler.py — DS 전용 Amazon 키워드 크롤러 (Phase 1+ Gap Score 데이터 수집).

목적:
  GO 후보 상품의 키워드로 Amazon 검색 페이지(/s?k=) 를 크롤링해
  amazon_search_results (개별 리스팅) + amazon_search_agg (집계) 테이블을 채운다.
  scoring_service.calculate_gap_score 가 이 집계 데이터를 소비한다.

차단 방지 정책 (작업지시서 명시):
  - 요청 간 15-25초 랜덤 딜레이
  - 50 키워드마다 2-3분 쿨다운
  - CAPTCHA 감지 시 5분 대기
  - 연속 실패 3회 → 10분 정지, 5회 → 중단
  - User-Agent 로테이션
  - Webshare 프록시 US 10 IP (셀러 계정과 IP 분리)

EC2 의존: PROXY_* 환경변수 + Playwright Chromium.
"""
import logging
import random
import re
import time
from typing import Optional
from urllib.parse import quote_plus

import requests

from backend_shared.utils.proxy_pool import get_default_pool
from backend_shared.utils.rate_limiter import CrawlerDelayer
from backend.dropshipping.database import get_db

logger = logging.getLogger(__name__)


USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7; rv:124.0) Gecko/20100101 Firefox/124.0",
]


def _make_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "sec-ch-ua": '"Chromium";v="123", "Not.A/Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-site": "none",
        "sec-fetch-mode": "navigate",
        "sec-fetch-user": "?1",
        "sec-fetch-dest": "document",
    }


def _detect_captcha(html: str) -> bool:
    return (
        "captcha" in html.lower()
        or "Enter the characters you see below" in html
        or "Robot Check" in html
    )


def _parse_search_page(html: str) -> list[dict]:
    """검색 결과 페이지 HTML → 개별 리스팅 dict 리스트.

    DOM 의존 (data-component-type="s-search-result").
    실패 시 빈 리스트.
    """
    items = []
    # 거친 정규식 파싱 — Playwright 사용 시 더 정확한 selector 로 교체
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

    Returns: {"crawled": N, "aborted": bool, "captcha_count": N}
    """
    if delayer is None:
        delayer = CrawlerDelayer(name="amazon_keyword_crawler")

    pool = get_default_pool()
    captcha_count = 0
    crawled = 0

    for kw in keywords:
        if not delayer.before_request():
            logger.error("크롤러 중단 — 연속 실패 임계값 초과")
            break

        url = f"https://www.amazon.com/s?k={quote_plus(kw)}&ref=nb_sb_noss"
        try:
            r = requests.get(
                url,
                headers=_make_headers(),
                proxies=pool.get(),
                timeout=20,
                allow_redirects=True,
            )

            if r.status_code != 200:
                logger.warning(f"[{kw}] HTTP {r.status_code}")
                delayer.report_failure()
                continue

            if _detect_captcha(r.text):
                captcha_count += 1
                logger.warning(f"[{kw}] CAPTCHA 감지")
                delayer.report_failure(captcha=True)
                continue

            items = _parse_search_page(r.text)
            agg = _aggregate(items)

            _save_results(kw, items, agg)
            delayer.report_success()
            crawled += 1
            logger.info(f"[{kw}] 수집 {len(items)}개, p75=${agg['price_p75']}")

        except Exception as e:
            logger.error(f"[{kw}] 크롤 실패: {e}")
            delayer.report_failure()

    return {
        "crawled": crawled,
        "aborted": delayer.aborted,
        "captcha_count": captcha_count,
        "total_keywords": len(keywords),
    }


def _save_results(keyword: str, items: list[dict], agg: dict) -> None:
    with get_db() as conn:
        # 개별 리스팅
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

        # 키워드 집계
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
