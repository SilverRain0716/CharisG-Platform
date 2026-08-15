"""
google_trends_service.py — Google Trends 데이터 수집
미국 급상승 키워드 + 카테고리별 인기 검색어 + 관련 키워드 확장
"""
import logging
import time
from pytrends.request import TrendReq

logger = logging.getLogger(__name__)

_last_call = 0
CALL_DELAY = 2


def _get_pytrends() -> TrendReq:
    global _last_call
    now = time.time()
    if now - _last_call < CALL_DELAY:
        time.sleep(CALL_DELAY - (now - _last_call))
    _last_call = time.time()
    return TrendReq(hl='en-US', tz=300, timeout=(10, 25))


def get_trending_searches(country: str = "united_states") -> list[str]:
    try:
        pt = _get_pytrends()
        df = pt.trending_searches(pn=country)
        keywords = df[0].tolist()[:30]
        logger.info(f"📈 Google Trends 급상승 {len(keywords)}개 키워드 수집 ({country})")
        return keywords
    except Exception as e:
        logger.error(f"Google Trends 급상승 수집 실패: {e}")
        return []


def get_related_queries(keyword: str, geo: str = "US") -> dict:
    try:
        pt = _get_pytrends()
        pt.build_payload([keyword], cat=0, timeframe='now 7-d', geo=geo)
        related = pt.related_queries()
        result = {"rising": [], "top": []}
        if keyword in related:
            rising = related[keyword].get("rising")
            top = related[keyword].get("top")
            if rising is not None and not rising.empty:
                result["rising"] = rising["query"].tolist()[:10]
            if top is not None and not top.empty:
                result["top"] = top["query"].tolist()[:10]
        return result
    except Exception as e:
        logger.error(f"관련 검색어 수집 실패 ({keyword}): {e}")
        return {"rising": [], "top": []}


def get_interest_over_time(keywords: list[str], timeframe: str = "now 7-d", geo: str = "US") -> list[dict]:
    if not keywords:
        return []
    try:
        pt = _get_pytrends()
        kw_batch = keywords[:5]
        pt.build_payload(kw_batch, cat=0, timeframe=timeframe, geo=geo)
        df = pt.interest_over_time()
        if df.empty:
            return []
        result = []
        for idx, row in df.iterrows():
            entry = {"date": idx.strftime("%Y-%m-%d")}
            for kw in kw_batch:
                entry[kw] = int(row.get(kw, 0))
            result.append(entry)
        return result
    except Exception as e:
        logger.error(f"검색 관심도 수집 실패: {e}")
        return []
