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

파싱 리팩터 (2026-04-22, Stage 3-A):
  _parse_search_page 를 검증된 KR/US 듀얼 통화 정규식으로 교체.
  _aggregate 를 region-aware 로 변경 (KR → KRW만, US → USD만).
  _save_results INSERT 에 rating 컬럼 추가. currency/kr_shipping/price_display 는
  현재 스키마에 컬럼 부재로 DB 미저장 — 메모리·로그·집계에만 반영.

크레딧 / 비용:
  premium_proxy=true (+ proxy_country=kr), js_render 미사용 기준 요청당 약 5 크레딧.

CAPTCHA / 차단:
  ZenRows 가 자동 재시도 + 프록시 로테이션 수행. 클라이언트 측 딜레이는 최소화
  (내부 fallback delayer: 5-10s 간격).

EC2 의존: ZENROWS_API_KEY 환경변수만 필요. 프록시 없음.
"""
import logging
import os
import re
import time
from html import unescape
from typing import Literal, Optional
from urllib.parse import quote_plus

import requests

from backend_shared.utils.rate_limiter import CrawlerDelayer
from backend.dropshipping.database import get_db

logger = logging.getLogger(__name__)


ZENROWS_API_KEY = os.getenv("ZENROWS_API_KEY", "")
ZENROWS_ENDPOINT = "https://api.zenrows.com/v1/"


def _zenrows_get(
    amazon_url: str,
    *,
    proxy_country: Optional[str] = None,
    timeout: int = 60,
) -> requests.Response:
    """Amazon URL을 ZenRows API 경유로 페치.

    요청당 ~5 크레딧(premium_proxy=true, js_render 미사용).
    ZenRows가 프록시 로테이션/재시도/WAF 우회를 자동 수행.

    proxy_country: 'kr' 지정 시 한국에서 접속하는 것으로 간주되어 Amazon.com이
      KRW 가격과 "Republic of Korea" 배송 정보를 노출. 미지정 시 기본 US.
    """
    if not ZENROWS_API_KEY:
        raise RuntimeError("ZENROWS_API_KEY not configured in environment")

    params = {
        "apikey": ZENROWS_API_KEY,
        "url": amazon_url,
        "premium_proxy": "true",
    }
    if proxy_country:
        params["proxy_country"] = proxy_country
    return requests.get(ZENROWS_ENDPOINT, params=params, timeout=timeout)


def _detect_blocked(html: str) -> bool:
    """Amazon 차단/에러 페이지 감지."""
    lower = html.lower()
    return (
        "captcha" in lower
        or "Enter the characters you see below" in html
        or "Robot Check" in html
        or "Sorry! Something went wrong" in html
        or "challenge-container" in html
        or "AwsWafIntegration" in html
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Parser — KR/US HTML 양쪽 검증된 정규식 (Stage 3-A)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _extract_cards(html: str) -> list[tuple[str, str]]:
    """검색 결과 HTML → (ASIN, 카드 HTML slice) 튜플 리스트.

    s-result-item + data-asin div 의 시작 위치를 모아 다음 카드 시작 직전까지 slice.
    중복 ASIN 은 첫 등장만 보존.
    """
    positions: list[tuple[str, int]] = []
    for m in re.finditer(r"<div\b([^>]*)>", html):
        attrs = m.group(1)
        if "s-result-item" not in attrs:
            continue
        am = re.search(r'data-asin="([A-Z0-9]{10})"', attrs)
        if not am:
            continue
        positions.append((am.group(1), m.start()))

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for i, (asin, pos) in enumerate(positions):
        if asin in seen:
            continue
        seen.add(asin)
        end = positions[i + 1][1] if i + 1 < len(positions) else min(pos + 15000, len(html))
        out.append((asin, html[pos:end]))
    return out


def _detect_kr_shipping(card: str) -> str:
    """KR 배송 가능 여부. 'OK' / 'NO' / 'UNKNOWN'.

    v3.4 프롬프트 §2-3 검증 규칙:
      - 'cannot be shipped to' 또는 'does not ship to' → NO
      - 'FREE delivery|Delivery ... to (the )?Republic of Korea' (중간 날짜 span 허용) → OK
      - 'Ships to Korea' → OK
      - 기타 → UNKNOWN
    """
    if re.search(r"cannot be shipped to", card, re.I):
        return "NO"
    if "does not ship to" in card.lower():
        return "NO"
    if re.search(
        r"(FREE delivery|Delivery)[\s\S]{0,200}?to (the )?Republic of Korea",
        card,
        re.I,
    ):
        return "OK"
    if "Ships to Korea" in card:
        return "OK"
    return "UNKNOWN"


def _parse_card(asin: str, card: str) -> dict:
    """단일 카드 → 필드 dict. KR/US 양쪽 검증 완료."""
    info: dict = {
        "asin": asin,
        "title": "",
        "price_num": None,
        "price_display": None,
        "currency": None,
        "rating": None,
        "reviews": 0,
        "bought_monthly": None,
        "kr_shipping": "UNKNOWN",
        "is_fbm": False,  # 새 파서에서 미추출 (스키마 호환용 placeholder)
    }

    # Title — h2 내부 span 중 가장 긴 것 (카드에 여러 span 섞여도 본 타이틀 선정)
    h2 = re.findall(r"<h2[^>]*>.*?<span[^>]*>([^<]+)</span>", card, re.DOTALL)
    if h2:
        info["title"] = unescape(max(h2, key=len)).strip()

    # Price — USD / KRW 텍스트 / ₩ 심볼 세 가지 모두 대응
    p = re.search(
        r'<span class="a-offscreen">'
        r'(?:\$([\d,]+\.\d{2})|KRW\s+([\d,]+)|₩([\d,]+))'
        r'</span>',
        card,
    )
    if p:
        if p.group(1):
            info["price_display"] = f"${p.group(1)}"
            info["price_num"] = float(p.group(1).replace(",", ""))
            info["currency"] = "USD"
        elif p.group(2):
            info["price_display"] = f"KRW {p.group(2)}"
            info["price_num"] = float(p.group(2).replace(",", ""))
            info["currency"] = "KRW"
        elif p.group(3):
            info["price_display"] = f"₩{p.group(3)}"
            info["price_num"] = float(p.group(3).replace(",", ""))
            info["currency"] = "KRW"

    # Rating (X.Y out of 5 stars)
    rt = re.search(r"(\d\.\d)\s*out of 5 stars", card)
    if rt:
        info["rating"] = float(rt.group(1))

    # Review count (aria-label="12,345 ratings")
    rv = re.search(r'aria-label="([\d,]+)\s*ratings?"', card)
    if rv:
        info["reviews"] = int(rv.group(1).replace(",", ""))

    # Bought in past month ("2K+", "500+", "1M+" 등)
    bo = re.search(r">([\d,]+[KM]?\+?)\s*bought in past month<", card)
    if bo:
        info["bought_monthly"] = bo.group(1)

    info["kr_shipping"] = _detect_kr_shipping(card)
    return info


def _parse_search_page(html: str) -> list[dict]:
    """검색 결과 HTML → 카드별 dict 리스트."""
    return [_parse_card(asin, card) for asin, card in _extract_cards(html)]


def _is_1k_plus(marker: Optional[str]) -> bool:
    """'2K+', '1.5K+', '3M+' 등 → True. 숫자만('500') → False."""
    return bool(marker) and ("K" in marker or "M" in marker)


def _aggregate(items: list[dict], region: Literal["US", "KR"] = "US") -> dict:
    """개별 리스팅 → 키워드 집계 (region-aware).

    region='US' → USD 가격만 quartile 계산.
    region='KR' → KRW 가격만 quartile 계산.
    비가격 필드(rating, reviews, kr_shipping)는 전체 items 기준.
    """
    target_currency = "KRW" if region == "KR" else "USD"
    priced_items = [i for i in items if i.get("currency") == target_currency and i.get("price_num") is not None]
    prices = sorted([i["price_num"] for i in priced_items])

    def _pct(arr: list[float], p: float) -> Optional[float]:
        if not arr:
            return None
        idx = int(len(arr) * p)
        return arr[min(idx, len(arr) - 1)]

    ratings = [i["rating"] for i in items if i.get("rating") is not None]
    reviews = [i["reviews"] for i in items if i.get("reviews") is not None]

    kr_ok = sum(1 for i in items if i.get("kr_shipping") == "OK")
    kr_no = sum(1 for i in items if i.get("kr_shipping") == "NO")
    kr_unk = sum(1 for i in items if i.get("kr_shipping") == "UNKNOWN")

    return {
        "region": region,
        "currency": target_currency,
        "price_min": prices[0] if prices else None,
        "price_p25": _pct(prices, 0.25),
        "price_median": _pct(prices, 0.50),
        "price_p75": _pct(prices, 0.75),
        "price_max": prices[-1] if prices else None,
        "avg_review_count": float(sum(reviews) / len(reviews)) if reviews else 0.0,
        "min_review_count": min(reviews) if reviews else 0,
        "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
        # 새 파서에서 미추출 — 스키마 컬럼만 채움
        "prime_count": 0,
        "fba_count": 0,
        "fbm_count": 0,
        "total_results": len(items),
        "priced_results": len(priced_items),
        "bought_1k_plus_count": sum(1 for i in items if _is_1k_plus(i.get("bought_monthly"))),
        "kr_shipping_ok_count": kr_ok,
        "kr_shipping_no_count": kr_no,
        "kr_shipping_unknown_count": kr_unk,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Crawl loop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def crawl_keywords(
    keywords: list[str],
    delayer: Optional[CrawlerDelayer] = None,
    region: Literal["US", "KR"] = "US",
) -> dict:
    """키워드 리스트를 순차 크롤링 → DB 저장.

    region:
      'US' (기본) — Amazon.com 표준 (USD, 일반 US 배송)
      'KR'       — ZenRows proxy_country='kr' → KRW 가격 + Republic of Korea 배송
    """
    if delayer is None:
        delayer = CrawlerDelayer(
            delay_min=5.0,
            delay_max=10.0,
            name=f"amazon_keyword_crawler_zr_{region.lower()}",
        )

    proxy_country = "kr" if region == "KR" else None
    captcha_count = 0
    crawled = 0

    for kw in keywords:
        if not delayer.before_request():
            logger.error(f"⛔ [ZenRows][{region}] 크롤러 중단 — 연속 실패 임계값 초과")
            break

        url = f"https://www.amazon.com/s?k={quote_plus(kw)}&ref=nb_sb_noss"
        try:
            r = _zenrows_get(url, proxy_country=proxy_country, timeout=60)

            if r.status_code != 200:
                logger.warning(f"[ZenRows][{region}][{kw}] HTTP {r.status_code}")
                delayer.report_failure()
                continue

            size_kb = len(r.content) // 1024

            if _detect_blocked(r.text):
                captcha_count += 1
                logger.warning(f"[ZenRows][{region}][{kw}] 차단/CAPTCHA 마커 감지 (size={size_kb}KB)")
                delayer.report_failure(captcha=True)
                continue

            items = _parse_search_page(r.text)
            if not items:
                logger.warning(f"[ZenRows][{region}][{kw}] 파싱 결과 0건 (size={size_kb}KB)")
                delayer.report_failure()
                continue

            agg = _aggregate(items, region=region)
            _save_results(kw, items, agg)
            delayer.report_success()
            crawled += 1
            logger.info(
                f"[ZenRows][{region}][{kw}] 수집 {len(items)}개 "
                f"(가격 {agg['priced_results']}/{len(items)} {agg['currency']}), "
                f"p75={agg['price_p75']}, rating_avg={agg['avg_rating']}, "
                f"KR_OK={agg['kr_shipping_ok_count']}, 1K+={agg['bought_1k_plus_count']}, "
                f"size={size_kb}KB"
            )

        except Exception as e:
            logger.error(f"[ZenRows][{region}][{kw}] 크롤 실패: {e}")
            delayer.report_failure()

    return {
        "crawled": crawled,
        "aborted": delayer.aborted,
        "captcha_count": captcha_count,
        "total_keywords": len(keywords),
        "region": region,
    }


def _save_results(keyword: str, items: list[dict], agg: dict) -> None:
    with get_db() as conn:
        for it in items:
            if not it.get("asin"):
                continue
            conn.execute(
                """INSERT OR REPLACE INTO amazon_search_results
                   (keyword, asin, title, price, review_count, rating, is_fbm, collected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (
                    keyword,
                    it["asin"],
                    it.get("title", ""),
                    it.get("price_num"),
                    it.get("reviews", 0),
                    it.get("rating"),
                    1 if it.get("is_fbm") else 0,
                ),
            )

        conn.execute(
            """INSERT OR REPLACE INTO amazon_search_agg
               (keyword, price_min, price_p25, price_median, price_p75, price_max,
                avg_review_count, min_review_count, avg_rating,
                prime_count, fba_count, fbm_count, total_results, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (
                keyword,
                agg.get("price_min"), agg.get("price_p25"),
                agg.get("price_median"), agg.get("price_p75"), agg.get("price_max"),
                agg.get("avg_review_count", 0), agg.get("min_review_count", 0),
                agg.get("avg_rating"),
                agg.get("prime_count", 0), agg.get("fba_count", 0), agg.get("fbm_count", 0),
                agg.get("total_results", 0),
            ),
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
