"""
render_coupang_banners.py — 쿠팡 상세페이지용 정적 배너 HTML을 PNG로 렌더.

사용법 (로컬 또는 EC2 어디서든 venv + playwright 세팅되어 있으면 실행):
    python3 -m backend.purchase.scripts.render_coupang_banners

입력: backend/purchase/templates/coupang_banners_src/banner_*.html
출력: backend/purchase/media/banners/banner_*.jpg

렌더 옵션:
- 뷰포트 860px (상세페이지 표준 폭)
- full_page 스크린샷 (전체 세로 높이 캡처)
- Noto Sans KR 웹폰트 로드 대기 (networkidle)
- JPEG 85% (용량 최적화)
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]  # backend/purchase/
SRC_DIR = ROOT / "templates" / "coupang_banners_src"
OUT_DIR = ROOT / "media" / "banners"

BANNERS = [
    ("banner_1_brand.html", "banner_1_brand.jpg"),
    ("banner_2_shipping.html", "banner_2_shipping.jpg"),
    ("banner_3_amazon.html", "banner_3_amazon.jpg"),
    ("banner_4_purchase_notice.html", "banner_4_purchase_notice.jpg"),
    ("banner_5_customs.html", "banner_5_customs.jpg"),
]


async def _content_height(page) -> int:
    """문서 실제 렌더 높이(px). 여백 없는 스크린샷을 위한 뷰포트 산정용."""
    return await page.evaluate(
        "Math.ceil(document.documentElement.getBoundingClientRect().height)"
    )


async def render_all():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        context = await browser.new_context(
            viewport={"width": 860, "height": 1200},
            device_scale_factor=2,  # 2x for retina quality
        )
        for src_name, out_name in BANNERS:
            src = SRC_DIR / src_name
            out = OUT_DIR / out_name
            if not src.exists():
                print(f"[skip] {src} 없음")
                continue
            page = await context.new_page()
            await page.goto(src.as_uri(), wait_until="networkidle")
            # 추가 여유 (웹폰트 깜빡임 방지)
            await page.wait_for_timeout(500)
            # ★2026-08-07 여백 버그: full_page 는 뷰포트 높이를 하한으로 잡아, 콘텐츠가
            #   1200px 보다 짧은 배너 아래에 흰 여백을 붙여 내보냈다(banner_5: 144px
            #   콘텐츠가 1200px 로 출력, 여백 1,056px). 촬영 직전 뷰포트를 콘텐츠
            #   실높이에 맞춰 여백을 제거한다. 리사이즈가 리플로우를 유발할 수 있어
            #   높이가 안정될 때까지 최대 3회 재측정한다.
            h = await _content_height(page)
            for _ in range(3):
                await page.set_viewport_size({"width": 860, "height": max(1, h)})
                await page.wait_for_timeout(120)
                h2 = await _content_height(page)
                if h2 == h:
                    break
                h = h2
            await page.screenshot(
                path=str(out),
                full_page=True,
                type="jpeg",
                quality=88,
            )
            size_kb = out.stat().st_size / 1024
            print(f"[ok]   {src_name} → {out_name} ({size_kb:.0f} KB)")
            await page.close()
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(render_all())
