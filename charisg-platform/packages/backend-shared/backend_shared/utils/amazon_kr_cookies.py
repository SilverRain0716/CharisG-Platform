"""
amazon_kr_cookies.py — Amazon "Deliver to Republic of Korea" 익명 세션 쿠키 자동 갱신.

[DEPRECATED 2026-04-22]
  DS amazon_keyword_crawler 는 ZenRows proxy_country='kr' 로 전환되어 이 모듈이
  제공하던 KR 쿠키 주입이 더 이상 필요하지 않다. 모듈은 영구 fallback 목적으로
  보존 — ZenRows 장애 시 로컬 Playwright 경로로 복귀 가능.

DS/PA 크롤러가 region='KR' 모드에서 requests.Session.cookies 에 주입해 사용한다.

추출 순서 (2단계 fallback):
  1) Playwright로 amazon.com 접속 → Deliver to dropdown → South Korea 선택
  2) 실패 시 POST /portal-migration/hz/glow/address-change (zipCode=06000)
  둘 다 실패하면 명시적 예외 발생. 수동 복구 절차는 .cache/amazon_kr_cookies.json 직접 편집.

캐시: {CHARISG_ROOT}/.cache/amazon_kr_cookies.json — 7일 TTL (파일 mtime 기준).

프록시: 쿠키 추출 단계에서 Webshare US 10 IP 중 1개 사용.

Known Limitation:
  - Cookie extraction and subsequent crawling use different proxy IPs from
    the same Webshare pool (rotation is random per request).
  - Amazon may flag this IP mismatch as bot behavior.
  - If KR mode crawling shows elevated 503/CAPTCHA rates in production,
    consider implementing sticky session (reuse same proxy IP for extraction
    + subsequent crawl session).
  - Tracking: document in ops runbook under "KR crawler troubleshooting".

표준 실행:
    python -m backend_shared.utils.amazon_kr_cookies

## Conditional Upgrade Plan for API Fallback (added 2026-04-22)

API fallback path (_refresh_via_api) is currently DISABLED because it
silently fails validation (HTTP 200 but no KR mode applied).

Re-enablement triggers (reactivate when ANY of these occur):

  1. UI-based path breaks 3+ times in production within 60 days
     → API fallback becomes valuable even if imperfect

  2. KR-based proxy infrastructure is added to the system
     → Suspected root cause (US IP rejection) disappears

  3. Amazon web changes and a spot-check shows the API endpoint
     now produces actual KR mode (monthly manual test suggested)

Re-enablement procedure:
  - Test _refresh_via_api() standalone first
  - Verify Republic of Korea marker count >= 3 in subsequent page
  - If verified, remove the `raise RuntimeError` in refresh_kr_cookies()
    and restore the try/except fallback chain
  - Update this docstring
"""
import asyncio
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Optional

from backend_shared._config import (
    PROJECT_ROOT,
    PROXY_HOST,
    PROXY_PORT,
    PROXY_USER_BASE,
    PROXY_PASSWORD,
)

logger = logging.getLogger(__name__)

CACHE_PATH: Path = PROJECT_ROOT / ".cache" / "amazon_kr_cookies.json"
CACHE_TTL_SEC: int = 7 * 24 * 3600

SENSITIVE_COOKIES: set[str] = {"at-main", "sess-at-main", "x-main", "sst-main"}
REQUIRED_COOKIES: set[str] = {"session-id", "session-token", "sp-cdn"}

KR_ZIP: str = "06000"
AMAZON_HOME: str = "https://www.amazon.com/"
_UA: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)


def _pick_proxy_slot() -> Optional[int]:
    """Webshare US 풀에서 IP 슬롯(1-10) 랜덤 선택. 프록시 미설정 시 None."""
    if not all([PROXY_HOST, PROXY_PORT, PROXY_USER_BASE, PROXY_PASSWORD]):
        return None
    return random.randint(1, 10)


def _playwright_proxy(slot: int) -> dict:
    return {
        "server": f"http://{PROXY_HOST}:{PROXY_PORT}",
        "username": f"{PROXY_USER_BASE}-{slot}",
        "password": PROXY_PASSWORD,
    }


def _requests_proxies(slot: int) -> dict:
    url = f"http://{PROXY_USER_BASE}-{slot}:{PROXY_PASSWORD}@{PROXY_HOST}:{PROXY_PORT}"
    return {"http": url, "https": url}


def _filter_cookies(cookies: list[dict]) -> dict:
    return {
        c["name"]: c["value"]
        for c in cookies
        if c.get("name") and c["name"] not in SENSITIVE_COOKIES
    }


def _validate(cookies: dict) -> None:
    """필수 쿠키 존재 + sp-cdn 값에 'KR' 마커 포함 검증.

    sp-cdn 이 'L5Z9:US' 같은 US 기본값으로 추출되면 UI/API 조작이 silent fail 한 것.
    존재만 체크하면 이 케이스를 감지 못함 → 반드시 값 내용까지 검증.
    """
    missing = REQUIRED_COOKIES - cookies.keys()
    if missing:
        raise RuntimeError(f"KR 쿠키 필수 항목 누락: {sorted(missing)}")
    sp_cdn = cookies.get("sp-cdn", "")
    if "KR" not in sp_cdn:
        raise ValueError(
            f"sp-cdn does not contain KR marker (got: {sp_cdn!r}). "
            f"Deliver-to-Korea setting likely failed."
        )


async def _refresh_via_playwright() -> dict:
    """UI 자동화 — Deliver to 모달에서 South Korea 선택 + 이중 검증."""
    from playwright.async_api import async_playwright

    slot = _pick_proxy_slot()
    proxy = _playwright_proxy(slot) if slot else None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, proxy=proxy)
        try:
            ctx = await browser.new_context(locale="en-US", user_agent=_UA)
            page = await ctx.new_page()
            await page.goto(AMAZON_HOME, wait_until="domcontentloaded", timeout=45000)

            await page.click("#nav-global-location-popover-link", timeout=15000)
            await page.wait_for_selector("#GLUXCountryListDropdown", timeout=15000)

            try:
                await page.select_option("select#GLUXCountryListDropdown", value="KR")
            except Exception:
                await page.click("#GLUXCountryListDropdown")
                await page.click('a[data-value*="\\"KR\\""]', timeout=10000)

            await page.click(
                ".a-popover-footer .a-button-primary input, "
                "input[name='glowDoneButton'], "
                ".a-button-primary .a-button-input",
                timeout=10000,
            )
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await page.reload(wait_until="domcontentloaded", timeout=30000)

            # Primary 검증 — 헤더 배송지 슬롯 DOM 텍스트
            loc_el = await page.query_selector("#nav-global-location-slot, #glow-ingress-block")
            if not loc_el:
                raise RuntimeError(
                    "UI 자동화: #nav-global-location-slot / #glow-ingress-block DOM 엘리먼트 부재"
                )
            slot_text = (await loc_el.inner_text()) or ""
            if "Republic of Korea" not in slot_text and "Korea" not in slot_text:
                raise RuntimeError(
                    f"UI 자동화: 배송지 슬롯 텍스트에 Korea 마커 없음 (text={slot_text!r})"
                )

            # Secondary 검증 — HTML 전체 'Republic of Korea' 등장 횟수 ≥ 3
            content = await page.content()
            rok_count = content.count("Republic of Korea")
            if rok_count < 3:
                raise RuntimeError(
                    f"UI 자동화: HTML 'Republic of Korea' 마커 부족 (count={rok_count}, 요구=3+)"
                )

            raw = await ctx.cookies(AMAZON_HOME)
            cookies = _filter_cookies(raw)
            _validate(cookies)
            logger.info(
                f"✅ KR 쿠키 UI 자동화 추출 성공 ({len(cookies)}개) "
                f"via proxy {PROXY_HOST}:{PROXY_PORT} (slot {slot}, DOM+HTML rok_count={rok_count})"
            )
            return cookies
        finally:
            await browser.close()


def _refresh_via_api() -> dict:
    """
    [DISABLED 2026-04-22]

    API-based KR cookie refresh via
    POST /portal-migration/hz/glow/address-change.

    Currently disabled because the endpoint returns HTTP 200 but does not
    actually apply KR mode (verify step fails — 0 'Republic of Korea'
    markers in subsequent pages). Root cause suspected (unconfirmed):
    Amazon rejects KR shipping address changes from US-based IP addresses.

    This function is preserved for future re-activation when either:
      1. Amazon behavior changes (retry is free)
      2. KR-based proxies become available (root cause disappears)

    Do NOT call directly. Use refresh_kr_cookies() which routes to
    _refresh_via_playwright() only.
    """
    import requests

    slot = _pick_proxy_slot()
    proxies = _requests_proxies(slot) if slot else None

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    r = sess.get(AMAZON_HOME, proxies=proxies, timeout=25)
    r.raise_for_status()
    m = (
        re.search(r'"anti-csrftoken-a2z"\s*:\s*"([^"]+)"', r.text)
        or re.search(r'name="anti-csrftoken-a2z"\s+value="([^"]+)"', r.text)
    )
    csrf = m.group(1) if m else ""

    headers = {
        "anti-csrftoken-a2z": csrf,
        "x-requested-with": "XMLHttpRequest",
        "Referer": AMAZON_HOME,
        "Origin": "https://www.amazon.com",
    }
    body = {
        "locationType": "LOCATION_INPUT",
        "zipCode": KR_ZIP,
        "deviceType": "web",
        "storeContext": "generic",
        "pageType": "Gateway",
        "actionSource": "glow",
    }
    r2 = sess.post(
        "https://www.amazon.com/portal-migration/hz/glow/address-change",
        headers=headers, data=body, proxies=proxies, timeout=25,
    )
    r2.raise_for_status()

    sess.post(
        "https://www.amazon.com/gp/delivery/ajax/address-change.html",
        headers=headers,
        data={
            "locationType": "COUNTRY",
            "district": "KR",
            "countryCode": "KR",
            "deviceType": "web",
            "storeContext": "generic",
            "pageType": "Gateway",
            "actionSource": "glow",
        },
        proxies=proxies, timeout=25,
    )

    # 실증 verify — 동일 session 으로 홈 재요청해서 KR 마커 직접 확인
    verify = sess.get(AMAZON_HOME, proxies=proxies, timeout=25)
    if verify.status_code != 200:
        raise RuntimeError(f"API fallback verify: HTTP {verify.status_code}")
    rok_count = verify.text.count("Republic of Korea")
    if rok_count < 3:
        raise RuntimeError(
            f"API fallback cookies do not produce KR mode "
            f"(Republic of Korea count: {rok_count}, 요구=3+)"
        )

    cookies = {c.name: c.value for c in sess.cookies if c.name not in SENSITIVE_COOKIES}
    _validate(cookies)
    logger.info(
        f"✅ KR 쿠키 API fallback 추출 성공 ({len(cookies)}개) "
        f"via proxy {PROXY_HOST}:{PROXY_PORT} (slot {slot}, rok_count={rok_count})"
    )
    return cookies


async def refresh_kr_cookies() -> dict:
    """KR 쿠키 신규 추출 — UI 자동화 경로만 사용 (API fallback은 2026-04-22부로 비활성화)."""
    try:
        return await _refresh_via_playwright()
    except Exception as ui_err:
        # API fallback is disabled since 2026-04-22.
        # See "Conditional Upgrade Plan" in module docstring for re-enablement triggers.
        raise RuntimeError(
            f"UI-based KR cookie refresh failed: {ui_err}. "
            f"API fallback is currently disabled. "
            f"Manual recovery: edit {CACHE_PATH} directly."
        ) from ui_err


def _save_cache(cookies: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))


def load_cached_cookies() -> Optional[dict]:
    """캐시 로드 — 파일 없음/만료/검증 실패 시 None."""
    if not CACHE_PATH.exists():
        return None
    age = time.time() - CACHE_PATH.stat().st_mtime
    if age > CACHE_TTL_SEC:
        logger.info(f"KR 쿠키 캐시 만료 ({age / 86400:.1f}일 경과)")
        return None
    try:
        cookies = json.loads(CACHE_PATH.read_text())
        _validate(cookies)
        return cookies
    except Exception as e:
        logger.warning(f"KR 쿠키 캐시 로드 실패: {e}")
        return None


async def get_kr_cookies(force_refresh: bool = False) -> dict:
    """캐시 우선 조회, 없거나 만료거나 force_refresh 시 신규 추출 + 저장."""
    if not force_refresh:
        cached = load_cached_cookies()
        if cached:
            return cached
    cookies = await refresh_kr_cookies()
    _save_cache(cookies)
    return cookies


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = asyncio.run(get_kr_cookies(force_refresh=True))
    print(json.dumps(result, ensure_ascii=False, indent=2))
