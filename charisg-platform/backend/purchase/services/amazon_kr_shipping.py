"""Amazon 한국 직배송 가능 여부 확인 서비스.

Amazon.com 상품 페이지에 배송지를 한국(KR)으로 설정한 뒤
실제 배송 가능 여부를 HTML 패턴 매칭으로 판정한다.

사용처: sourcing_promote.py — promote 단계에서 한국 직배송 불가 상품을 걸러낸다.

동작 원리:
  1. Amazon 메인 접속 → 세션 쿠키 확보
  2. POST /portal-migration/hz/glow/address-change 로 배송지를 KR 로 변경
  3. 상품 페이지(dp/{ASIN}) HTML 에서 배송 가능/불가 패턴 매칭
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── 판정 패턴 ──────────────────────────────────────────────
_NEGATIVE_PATTERNS = [
    re.compile(r"this item cannot be shipped", re.I),
    re.compile(r"does not ship to", re.I),
    re.compile(r"cannot be delivered", re.I),
    re.compile(r"currently unavailable", re.I),
    re.compile(r"won'?t ship to", re.I),
    re.compile(r"not eligible for international", re.I),
    re.compile(r"cannot ship to", re.I),
]

_POSITIVE_PATTERNS = [
    re.compile(r"delivers?\s+to\s+(?:South\s+)?(?:Republic\s+of\s+)?Korea", re.I),
    re.compile(r"ships?\s+to\s+(?:South\s+)?Korea", re.I),
    re.compile(r"AmazonGlobal", re.I),
]

# rate limit: Amazon 연속 요청 시 차단 방지
_REQUEST_INTERVAL = 1.5  # 초


class AmazonKRShippingChecker:
    """세션을 유지하며 여러 ASIN의 한국 배송 가능 여부를 확인한다."""

    def __init__(self) -> None:
        self._client: Optional[httpx.Client] = None
        self._session_ready = False
        self._last_request_at = 0.0

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                headers=_HEADERS,
                follow_redirects=True,
                timeout=30.0,
            )
        return self._client

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < _REQUEST_INTERVAL:
            time.sleep(_REQUEST_INTERVAL - elapsed)
        self._last_request_at = time.monotonic()

    def init_session(self) -> bool:
        """Amazon 세션 쿠키 확보 + 배송지를 한국으로 변경.

        Returns:
            True if session initialized and location set to Korea.
        """
        client = self._ensure_client()

        # 1) 메인 페이지 → 세션 쿠키
        try:
            resp = client.get("https://www.amazon.com/")
            logger.info(f"Amazon 메인 접속: {resp.status_code}")
        except Exception as e:
            logger.error(f"Amazon 메인 접속 실패: {e}")
            return False

        # 2) 배송지를 KR로 변경
        try:
            change_resp = client.post(
                "https://www.amazon.com/portal-migration/hz/glow/address-change?actionSource=glow",
                data={
                    "locationType": "COUNTRY",
                    "district": "KR",
                    "countryCode": "KR",
                    "storeContext": "generic",
                    "deviceType": "web",
                    "pageType": "Gateway",
                    "actionSource": "glow",
                },
            )
            if change_resp.status_code == 200:
                self._session_ready = True
                logger.info("Amazon 배송지 → 한국(KR) 설정 완료")
                return True
            else:
                logger.warning(f"배송지 변경 응답: {change_resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"배송지 변경 실패: {e}")
            return False

    def check(self, asin: str) -> bool:
        """단일 ASIN의 한국 직배송 가능 여부 확인.

        Returns:
            True = 한국 직배송 가능, False = 불가 또는 확인 실패.
        """
        if not self._session_ready:
            if not self.init_session():
                logger.warning(f"세션 미준비, {asin} 건너뜀 (배송 불가 처리)")
                return False

        self._rate_limit()
        client = self._ensure_client()

        try:
            resp = client.get(f"https://www.amazon.com/dp/{asin}")
            html = resp.text
        except Exception as e:
            logger.warning(f"상품 페이지 요청 실패 ({asin}): {e}")
            return False

        if len(html) < 5000:
            logger.warning(f"상품 페이지 HTML 너무 짧음 ({asin}): {len(html)} bytes — CAPTCHA 의심")
            return False

        # 음성 신호(배송 불가) 우선 검사
        for pat in _NEGATIVE_PATTERNS:
            if pat.search(html):
                logger.info(f"[{asin}] 한국 직배송 불가 — 매칭: {pat.pattern}")
                return False

        # 양성 신호(배송 가능)
        for pat in _POSITIVE_PATTERNS:
            if pat.search(html):
                logger.info(f"[{asin}] 한국 직배송 가능 — 매칭: {pat.pattern}")
                return True

        # 양성/음성 모두 없으면 배송지 + Add to Cart 폴백
        has_cart = 'id="add-to-cart-button"' in html
        is_korea = bool(re.search(r"[Kk]orea", html[:5000]))  # 상단 배송지 영역만
        if has_cart and is_korea:
            logger.info(f"[{asin}] 한국 직배송 가능 (폴백: 카트버튼+배송지)")
            return True

        logger.info(f"[{asin}] 한국 직배송 불가 (신호 없음)")
        return False

    def check_batch(self, asins: list[str]) -> dict[str, bool]:
        """여러 ASIN을 한 번에 확인. 세션 1회 초기화 후 순차 처리."""
        if not self._session_ready:
            self.init_session()

        results = {}
        for asin in asins:
            results[asin] = self.check(asin)
        return results

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
            self._session_ready = False

    def __enter__(self):
        self.init_session()
        return self

    def __exit__(self, *exc):
        self.close()


def check_kr_shipping(asin: str) -> bool:
    """단건 확인 편의 함수. 매 호출마다 세션을 새로 만든다.
    배치 처리 시에는 AmazonKRShippingChecker 직접 사용 권장.
    """
    with AmazonKRShippingChecker() as checker:
        return checker.check(asin)
