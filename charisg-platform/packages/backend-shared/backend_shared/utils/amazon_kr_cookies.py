"""
amazon_kr_cookies.py — Amazon "Deliver to Republic of Korea" 익명 세션 쿠키 자동 갱신.

DS/PA 크롤러가 region='KR' 모드에서 requests.Session.cookies 에 주입해 사용한다.

추출 순서 (2단계 fallback):
  1) Playwright로 amazon.com 접속 → Deliver to dropdown → South Korea 선택
  2) 실패 시 POST /portal-migration/hz/glow/address-change (zipCode=06000)
  둘 다 실패하면 명시적 예외 발생. 수동 복구 절차는 .cache/amazon_kr_cookies.json 직접 편집.

캐시: {CHARISG_ROOT}/.cache/amazon_kr_cookies.json — 7일 TTL (파일 mtime 기준).

프록시: 쿠키 추출 단계에서도 Webshare US 10 IP 중 1개 사용 (실제 크롤과 IP 일관성).

표준 실행:
    python -m backend_shared.utils.amazon_kr_cookies
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


def _playwright_proxy() -> Optional[dict]:
    if not all([PROXY_HOST, PROXY_PORT, PROXY_USER_BASE, PROXY_PASSWORD]):
        return None
    ip_num = random.randint(1, 10)
    return {
        "server": f"http://{PROXY_HOST}:{PROXY_PORT}",
        "username": f"{PROXY_USER_BASE}-{ip_num}",
        "password": PROXY_PASSWORD,
    }


def _filter_cookies(cookies: list[dict]) -> dict:
    return {
        c["name"]: c["value"]
        for c in cookies
        if c.get("name") and c["name"] not in SENSITIVE_COOKIES
    }


def _validate(cookies: dict) -> None:
    missing = REQUIRED_COOKIES - cookies.keys()
    if missing:
        raise RuntimeError(f"KR 쿠키 필수 항목 누락: {sorted(missing)}")


async def _refresh_via_playwright() -> dict:
    """UI 자동화 — Deliver to 모달에서 South Korea 선택 + 검증."""
    from playwright.async_api import async_playwright

    proxy = _playwright_proxy()
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

            content = await page.content()
            if "Republic of Korea" not in content and "Korea, Republic" not in content:
                raise RuntimeError("UI 자동화 완료했으나 'Republic of Korea' 마커 미감지")

            raw = await ctx.cookies(AMAZON_HOME)
            cookies = _filter_cookies(raw)
            _validate(cookies)
            logger.info(f"✅ KR 쿠키 UI 자동화 추출 성공 ({len(cookies)}개)")
            return cookies
        finally:
            await browser.close()


def _refresh_via_api() -> dict:
    """fallback — requests 기반으로 /portal-migration/hz/glow/address-change 호출."""
    import requests

    from backend_shared.utils.proxy_pool import get_default_pool

    proxies = get_default_pool().get()
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

    verify = sess.get(AMAZON_HOME, proxies=proxies, timeout=25)
    if "Republic of Korea" not in verify.text and "Korea, Republic" not in verify.text:
        raise RuntimeError("API fallback 호출 완료했으나 'Republic of Korea' 마커 미감지")

    cookies = {c.name: c.value for c in sess.cookies if c.name not in SENSITIVE_COOKIES}
    _validate(cookies)
    logger.info(f"✅ KR 쿠키 API fallback 추출 성공 ({len(cookies)}개)")
    return cookies


async def refresh_kr_cookies() -> dict:
    """KR 쿠키 신규 추출 — UI 자동화 → API fallback 순서."""
    try:
        return await _refresh_via_playwright()
    except Exception as e_ui:
        logger.warning(f"⚠️ Playwright UI 자동화 실패 → API fallback: {e_ui}")
        try:
            return await asyncio.to_thread(_refresh_via_api)
        except Exception as e_api:
            raise RuntimeError(
                f"KR 쿠키 추출 2단계 모두 실패. "
                f"UI: {e_ui!r} / API: {e_api!r}. "
                f"수동 복구: {CACHE_PATH} 직접 편집."
            ) from e_api


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
