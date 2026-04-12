"""USD/KRW 환율 서비스 — 네이버 금융 무인증 엔드포인트.

Primary:  api.stock.naver.com/marketindex/exchange/FX_USDKRW  (JSON, 하나은행 고시회차)
Fallback: finance.naver.com/marketindex/                     (HTML 스크래핑, 동일 값)

저장은 settings 테이블의 두 key:
  - exchange_rate_usd_krw       (float 문자열)
  - exchange_rate_updated_at    (ISO-8601 UTC)

비공식 API 함정:
  - 짧은 User-Agent 는 봇 감지로 403/빈 응답. Chrome full UA 필수.
  - 1000 < rate < 2000 sanity check — 벗어나면 파싱 버그.
"""
import logging
import re
from datetime import datetime

import requests

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HEADERS_JSON = {"User-Agent": _UA, "Referer": "https://m.stock.naver.com/"}
_HEADERS_HTML = {"User-Agent": _UA, "Referer": "https://finance.naver.com/"}

_PRIMARY_URL = "https://api.stock.naver.com/marketindex/exchange/FX_USDKRW"
_FALLBACK_URL = "https://finance.naver.com/marketindex/"

_SANITY_MIN = 1000.0
_SANITY_MAX = 2000.0
_DEFAULT_RATE = 1430.0
_TIMEOUT = 10


def _sanity_check(rate: float) -> float:
    if not (_SANITY_MIN < rate < _SANITY_MAX):
        raise ValueError(f"환율 범위 이탈: {rate} (허용 {_SANITY_MIN}~{_SANITY_MAX})")
    return rate


def _fetch_primary() -> float:
    """하나은행 고시회차 JSON. exchangeInfo.calcPrice 를 float 로 변환."""
    r = requests.get(_PRIMARY_URL, headers=_HEADERS_JSON, timeout=_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    info = data.get("exchangeInfo") or {}
    raw = info.get("calcPrice") or info.get("closePrice")
    if not raw:
        raise ValueError(f"primary 응답에 calcPrice/closePrice 없음: keys={list(info.keys())[:10]}")
    rate = float(str(raw).replace(",", ""))
    return _sanity_check(rate)


_HTML_RE = re.compile(
    r'class="head usd".*?<span[^>]*class="value"[^>]*>([\d,\.]+)',
    re.DOTALL,
)


def _fetch_fallback() -> float:
    """finance.naver.com 메인 HTML 에서 USD value 스크래핑."""
    r = requests.get(_FALLBACK_URL, headers=_HEADERS_HTML, timeout=_TIMEOUT)
    r.raise_for_status()
    html = r.content.decode("euc-kr", errors="replace")
    m = _HTML_RE.search(html)
    if not m:
        raise ValueError("fallback HTML 에서 USD value 파싱 실패")
    rate = float(m.group(1).replace(",", ""))
    return _sanity_check(rate)


def fetch_latest_usd_krw() -> float:
    """Primary → Fallback 순으로 시도. 둘 다 실패 시 RuntimeError."""
    try:
        rate = _fetch_primary()
        logger.info(f"[fx] primary OK: {rate}")
        return rate
    except Exception as e:
        logger.warning(f"[fx] primary 실패: {e}")

    try:
        rate = _fetch_fallback()
        logger.info(f"[fx] fallback OK: {rate}")
        return rate
    except Exception as e:
        logger.error(f"[fx] fallback 실패: {e}")

    raise RuntimeError("네이버 금융 환율 조회 실패 (primary + fallback 모두)")


def update_and_store() -> dict:
    """환율 조회 + settings 테이블 저장. 성공 시 {rate, updated_at} 반환."""
    rate = fetch_latest_usd_krw()
    updated_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with get_db() as conn:
        conn.execute(
            "UPDATE settings SET value=? WHERE key='exchange_rate_usd_krw'",
            (f"{rate:.2f}",),
        )
        conn.execute(
            "UPDATE settings SET value=? WHERE key='exchange_rate_updated_at'",
            (updated_at,),
        )
    return {"rate": rate, "updated_at": updated_at}


def get_current_rate() -> float:
    """settings 에서 캐시된 환율 읽기. 없거나 파싱 실패 시 1430."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='exchange_rate_usd_krw'"
        ).fetchone()
    if not row or not row["value"]:
        return _DEFAULT_RATE
    try:
        return float(row["value"])
    except (TypeError, ValueError):
        return _DEFAULT_RATE


def get_updated_at() -> str:
    """settings 의 exchange_rate_updated_at. 없으면 빈 문자열."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='exchange_rate_updated_at'"
        ).fetchone()
    return row["value"] if row and row["value"] else ""
