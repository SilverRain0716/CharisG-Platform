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

def _build_search_url(kw: str, page: int) -> str:
    base = f"https://www.amazon.com/s?k={quote_plus(kw)}&ref=nb_sb_noss"
    if page > 1:
        base += f"&page={page}"
    return base


def _save_raw_response(kw: str, page: int, r: "requests.Response") -> None:
    """응답을 /tmp/zenrows_raw/ 에 저장. 상태별 suffix로 식별."""
    ts = int(time.time())
    safe_kw = kw.replace(" ", "_")
    if r.status_code != 200:
        suffix = f"HTTP{r.status_code}"
    elif len(r.content) < 500_000:
        suffix = f"SMALL{len(r.content)}"
    else:
        suffix = "OK"
    fname = f"/tmp/zenrows_raw/{safe_kw}_{page}_{ts}_{suffix}.html"
    try:
        with open(fname, "w") as f:
            f.write(r.text)
    except Exception as e:
        logger.warning(f"[{kw}] page={page} raw save failed: {e}")


def _fetch_page_with_retry(
    kw: str,
    page: int,
    proxy_country: Optional[str],
    delayer: CrawlerDelayer,
    save_raw_html: bool,
    max_retries: int = 1,
) -> tuple[bool, Optional["requests.Response"], str, int]:
    """단일 페이지 요청 + 재시도.

    Returns:
        (success, response, failure_reason, attempts_used)

    failure_reason: "" on success. 실패 시:
        "delayer_aborted" — delayer 중단으로 시도 불가
        "request_exception" — requests 예외 (네트워크 실패 등)
        "http_error" — r.status_code != 200
        "soft_block" — 응답 크기 < 500KB
    attempts_used: 실제 수행된 시도 수.

    save_raw_html=True 면 **사이즈 가드 이전**에 매 응답 저장 (소프트 블록 본문도 보존).
    retry 간 추가 sleep 없음 — 다음 `delayer.before_request()` 의 자체 슬립에 위임.
    """
    total_attempts = 1 + max_retries
    last_reason = ""
    last_response: Optional["requests.Response"] = None
    attempts_done = 0

    for attempt in range(1, total_attempts + 1):
        if not delayer.before_request():
            return False, last_response, "delayer_aborted", attempts_done

        attempts_done = attempt
        url = _build_search_url(kw, page)

        try:
            r = _zenrows_get(url, proxy_country=proxy_country, timeout=60)
        except Exception as e:
            logger.warning(
                f"[{kw}] page={page} attempt={attempt}/{total_attempts} "
                f"failed: request_exception ({e})"
            )
            delayer.report_failure()
            last_reason = "request_exception"
            last_response = None
            continue

        if save_raw_html:
            _save_raw_response(kw, page, r)

        if r.status_code != 200:
            logger.warning(
                f"[{kw}] page={page} attempt={attempt}/{total_attempts} "
                f"failed: HTTP {r.status_code}"
            )
            delayer.report_failure()
            last_reason = "http_error"
            last_response = r
            continue

        if len(r.content) < 500_000:
            logger.warning(
                f"[{kw}] page={page} attempt={attempt}/{total_attempts} "
                f"failed: soft_block ({len(r.content)} bytes)"
            )
            delayer.report_failure()
            last_reason = "soft_block"
            last_response = r
            continue

        delayer.report_success()
        if attempt > 1:
            logger.info(f"[{kw}] page={page} retry succeeded on attempt={attempt}")
        return True, r, "", attempt

    logger.warning(
        f"[{kw}] page={page} skipped after {total_attempts} attempts "
        f"(last={last_reason}), continuing to next page"
    )
    return False, last_response, last_reason, attempts_done


def crawl_keywords(
    keywords: list[str],
    delayer: Optional[CrawlerDelayer] = None,
    region: Literal["US", "KR"] = "US",
    max_pages: int = 1,
    smart_paginate: bool = False,
    save_raw_html: bool = False,
) -> dict:
    """키워드 리스트를 순차 크롤링 → DB 저장.

    region:
      'US' — Amazon.com 표준 (USD, deprecated)
      'KR' — ZenRows proxy_country='kr' → KRW 가격 + Republic of Korea 배송

    max_pages: 키워드당 크롤링 페이지 수 (1~10, 기본 1로 하위호환).
    smart_paginate: True 면 page>=4 에서 신규 1K+ ASIN <=2 면 조기 중단.
    save_raw_html: True 면 /tmp/zenrows_raw/ 에 매 응답 저장 (소프트 블록 포함).

    페이지 단위 retry (기본 1회) 내장. 단일 페이지가 retry 까지 실패하면
    해당 페이지만 skip 하고 다음 페이지 진행. 연속 2페이지 실패 시 키워드 중단.
    """
    if not (1 <= max_pages <= 10):
        raise ValueError(f"max_pages must be 1..10, got {max_pages}")

    if delayer is None:
        delayer = CrawlerDelayer(
            delay_min=5.0,
            delay_max=10.0,
            name=f"amazon_keyword_crawler_zr_{region.lower()}",
        )

    proxy_country = "kr" if region == "KR" else None
    run_start_time = time.time()
    per_keyword: dict = {}

    if save_raw_html:
        os.makedirs("/tmp/zenrows_raw", exist_ok=True)

    for kw in keywords:
        kw_start_time = time.time()
        seen_asins: set[str] = set()
        kw_items: list[dict] = []
        page_1k_counts: list[int] = []
        page_extracted_counts: list[int] = []
        page_sizes_kb: list[int] = []
        pages_failed: list[int] = []
        retry_count = 0
        page_consecutive_failures = 0
        pages_crawled = 0
        stopped_reason = "max_pages"

        for page in range(1, max_pages + 1):
            success, r, reason, attempts = _fetch_page_with_retry(
                kw, page, proxy_country, delayer,
                save_raw_html=save_raw_html, max_retries=1,
            )
            retry_count += max(0, attempts - 1)

            if reason == "delayer_aborted":
                stopped_reason = "delayer_aborted"
                break

            if not success:
                pages_failed.append(page)
                page_consecutive_failures += 1
                if page_consecutive_failures >= 2:
                    stopped_reason = "consecutive_failures"
                    logger.warning(
                        f"[{kw}] consecutive_failures={page_consecutive_failures}, "
                        f"stopping keyword"
                    )
                    break
                continue

            page_consecutive_failures = 0
            page_sizes_kb.append(len(r.content) // 1024)

            page_items = _parse_search_page(r.text)
            new_items = [it for it in page_items if it["asin"] not in seen_asins]
            seen_asins.update(it["asin"] for it in new_items)
            kw_items.extend(new_items)
            pages_crawled = page

            extracted = sum(1 for it in new_items if it.get("bought_monthly"))
            page_extracted_counts.append(extracted)
            cnt_1k = sum(1 for it in new_items if _is_1k_plus(it.get("bought_monthly")))
            page_1k_counts.append(cnt_1k)

            logger.info(
                f"[{kw}] page={page} items={len(page_items)} new={len(new_items)} "
                f"1K+={cnt_1k} bought_extracted={extracted}/{len(new_items)}"
            )

            if smart_paginate and page >= 4 and cnt_1k <= 2:
                stopped_reason = "smart_stop"
                break

        kw_duration = time.time() - kw_start_time
        if kw_items:
            agg = _aggregate(kw_items, region=region)
            _save_results(kw, kw_items, agg)
        else:
            agg = {}

        total_res = agg.get("total_results") or 0
        kr_ok = agg.get("kr_shipping_ok_count") or 0
        kr_ok_pct = round(100.0 * kr_ok / total_res, 1) if total_res else None

        per_keyword[kw] = {
            "pages": pages_crawled,
            "asins": len(kw_items),
            "kr_ok_pct": kr_ok_pct,
            "bought_monthly_extracted": sum(page_extracted_counts),
            "page_1k_counts": page_1k_counts,
            "page_extracted_counts": page_extracted_counts,
            "page_sizes_kb": page_sizes_kb,
            "pages_failed": pages_failed,
            "retry_count": retry_count,
            "stopped_reason": stopped_reason,
            "duration_seconds": round(kw_duration, 1),
        }

    total_pages = sum(v["pages"] for v in per_keyword.values())
    return {
        "per_keyword": per_keyword,
        "total_keywords": len(keywords),
        "total_pages": total_pages,
        "total_asins": sum(v["asins"] for v in per_keyword.values()),
        "total_credits_est": total_pages * 5,
        "duration_seconds": round(time.time() - run_start_time, 1),
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
