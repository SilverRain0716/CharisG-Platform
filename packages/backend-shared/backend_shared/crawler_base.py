"""
base.py — BaseCrawler 추상 클래스
모든 크롤러의 공통 기능: 프록시, 진행률 콜백, DB 저장, 에러 핸들링, 재시도
"""
import asyncio
import logging
import os
import random
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

from backend_shared.context import get_db
from backend_shared.log_service import add_log
from backend_shared.progress_service import broadcast_progress

load_dotenv()
logger = logging.getLogger(__name__)

# ── Proxy 설정
PROXY_HOST = os.environ.get("PROXY_HOST", "p.webshare.io")
PROXY_PORT = os.environ.get("PROXY_PORT", "80")
PROXY_USER_BASE = os.environ.get("PROXY_USER_BASE", "wthluxio-us")
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD", "")

PROXY_POOL = [
    {
        "server": f"http://{PROXY_HOST}:{PROXY_PORT}",
        "username": f"{PROXY_USER_BASE}-{i}",
        "password": PROXY_PASSWORD,
    }
    for i in range(1, 11)
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]


class BaseCrawler(ABC):
    """
    크롤러 공통 추상 클래스

    하위 클래스 필수 구현:
        async def parse_page(self, page, url_entry, page_num) -> list[dict]
        async def setup_browser(self, playwright, proxy) -> (browser, context)
    """

    name: str = "base"
    delay_min: float = 3
    delay_max: float = 8
    retry_count: int = 3

    def __init__(self, job_id: int):
        self.job_id = job_id
        self.job = None
        self.urls = []
        self.collected = 0
        self.failed = 0
        self.stopped = False

    # ── 공개 API ──

    async def run(self):
        """메인 실행 — crawl_jobs에서 URL 로드 → 순차 크롤링 → DB 저장"""
        self._load_job()
        if not self.job:
            return

        total_urls = len(self.urls)
        logger.info(f"🕷 [{self.name}] 크롤링 시작: job #{self.job_id} ({total_urls}개 URL)")
        self._update_job_status("running")
        self._broadcast({"type": "start", "total_urls": total_urls})

        for url_idx, url_entry in enumerate(self.urls):
            if self.stopped:
                break
            if self._check_stopped():
                break

            url = url_entry["url"]
            start_page = url_entry.get("start_page") or 1
            end_page = url_entry.get("end_page") or 1
            category = url_entry.get("category") or ""
            label = url_entry.get("label") or ""

            logger.info(f"🌐 [{self.name}] URL {url_idx+1}/{total_urls}: {url[:80]}...")
            self._update_url_status(url_entry["id"], "running")

            url_collected = 0
            try:
                for page_num in range(start_page, end_page + 1):
                    if self.stopped or self._check_stopped():
                        break

                    proxy = self._get_proxy()
                    items = await self._crawl_with_retry(url, url_entry, page_num, proxy)

                    if items:
                        saved = self._save_products(items, url_entry)
                        url_collected += saved
                        self.collected += saved

                    self._broadcast({
                        "type": "progress",
                        "url_index": url_idx,
                        "page_num": page_num,
                        "total_urls": total_urls,
                        "collected": self.collected,
                        "current_url_collected": url_collected,
                    })

                    if page_num < end_page:
                        await self._delay()

                self._update_url_status(url_entry["id"], "completed", url_collected)

            except Exception as e:
                logger.error(f"❌ [{self.name}] URL 크롤링 실패: {e}")
                self._update_url_status(url_entry["id"], "failed", url_collected, str(e))
                self.failed += 1

            # URL 간 딜레이
            if url_idx < total_urls - 1:
                await self._delay(min_sec=5, max_sec=12)

        # 완료
        final_status = "stopped" if self.stopped else "completed"
        self._update_job_status(final_status)
        self._broadcast({
            "type": "done",
            "status": final_status,
            "collected": self.collected,
            "failed": self.failed,
        })

        add_log(
            "ok" if final_status == "completed" else "warn",
            f"🏁 [{self.name}] 크롤링 {final_status}: {self.collected}개 수집, {self.failed}개 실패"
        )
        logger.info(f"🏁 [{self.name}] job #{self.job_id} {final_status}: {self.collected}개 수집")

    def stop(self):
        """외부에서 중단 요청"""
        self.stopped = True

    # ── 하위 클래스 필수 구현 ──

    @abstractmethod
    async def parse_page(self, page, url_entry: dict, page_num: int) -> list[dict]:
        """
        페이지 파싱 — 하위 클래스에서 구현

        Returns: list of dict, 각 dict는 collected_products 컬럼에 매핑되는 키:
            product_name, source_price, url, external_id, image_url,
            category, brand, rating, review_count, rank, rank_change, ...
        """
        ...

    @abstractmethod
    async def setup_browser(self, playwright, proxy: dict):
        """
        브라우저+컨텍스트 생성 — 하위 클래스에서 구현

        Returns: (browser, context)
        """
        ...

    # ── 내부 헬퍼 ──

    async def _crawl_with_retry(self, url: str, url_entry: dict, page_num: int, proxy: dict) -> list[dict]:
        """재시도 포함 크롤링"""
        from playwright.async_api import async_playwright

        for attempt in range(1, self.retry_count + 1):
            try:
                async with async_playwright() as pw:
                    browser, context = await self.setup_browser(pw, proxy)
                    try:
                        page = await context.new_page()
                        target_url = self._build_page_url(url, page_num)

                        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                        await self._delay(2, 5)

                        items = await self.parse_page(page, url_entry, page_num)

                        if items:
                            logger.info(f"📦 [{self.name}] p{page_num}: {len(items)}개 파싱")
                            return items

                        if attempt < self.retry_count:
                            logger.warning(f"⚠️ [{self.name}] p{page_num}: 0개 → 프록시 교체 재시도")
                            proxy = self._get_proxy()
                            await self._delay(8, 15)

                    finally:
                        await browser.close()

            except Exception as e:
                logger.error(f"❌ [{self.name}] p{page_num} attempt {attempt}/{self.retry_count}: {e}")
                if attempt < self.retry_count:
                    proxy = self._get_proxy()
                    await self._delay(8, 15)

        logger.error(f"💀 [{self.name}] p{page_num} 최종 실패")
        return []

    def _build_page_url(self, base_url: str, page_num: int) -> str:
        """페이지 URL 생성 — 하위 클래스에서 오버라이드 가능"""
        return base_url

    def _save_products(self, items: list[dict], url_entry: dict) -> int:
        """크롤링 결과 → collected_products DB 저장"""
        saved = 0
        with get_db() as conn:
            for item in items:
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO collected_products
                           (job_id, source, business_model, external_id, url,
                            product_name, category, brand, description,
                            image_url, images, source_price, source_currency,
                            review_count, rating, rank, rank_change,
                            stock_status, shipping_type,
                            processing_status, listing_status)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            self.job_id,
                            self.name,
                            self.job.get("business_model", "purchase_agent"),
                            item.get("external_id", ""),
                            item.get("url", ""),
                            item.get("product_name", ""),
                            item.get("category") or url_entry.get("category", ""),
                            item.get("brand", ""),
                            item.get("description", ""),
                            item.get("image_url", ""),
                            item.get("images", "[]"),
                            item.get("source_price"),
                            item.get("source_currency", "USD"),
                            item.get("review_count"),
                            item.get("rating"),
                            item.get("rank"),
                            item.get("rank_change"),
                            item.get("stock_status", "in_stock"),
                            url_entry.get("shipping_type", "direct"),
                            "raw",
                            "collected",
                        ),
                    )
                    saved += 1
                except Exception as e:
                    logger.warning(f"⚠️ DB 저장 실패: {e}")

            # crawl_jobs 카운터 업데이트
            conn.execute(
                "UPDATE crawl_jobs SET collected_products = ?, processed_urls = processed_urls + 0 WHERE id = ?",
                (self.collected + saved, self.job_id),
            )
        return saved

    def _load_job(self):
        """DB에서 job + urls 로드"""
        with get_db() as conn:
            row = conn.execute("SELECT * FROM crawl_jobs WHERE id = ?", (self.job_id,)).fetchone()
            if not row:
                logger.error(f"Job #{self.job_id} not found")
                return
            self.job = dict(row)
            urls = conn.execute(
                "SELECT * FROM crawl_job_urls WHERE job_id = ? AND status != 'completed' ORDER BY id",
                (self.job_id,),
            ).fetchall()
            self.urls = [dict(u) for u in urls]

    def _check_stopped(self) -> bool:
        """DB에서 중단 여부 확인"""
        with get_db() as conn:
            row = conn.execute("SELECT status FROM crawl_jobs WHERE id = ?", (self.job_id,)).fetchone()
            if row and row["status"] == "stopped":
                self.stopped = True
                return True
        return False

    def _update_job_status(self, status: str):
        with get_db() as conn:
            if status in ("completed", "stopped", "failed"):
                conn.execute(
                    "UPDATE crawl_jobs SET status=?, finished_at=CURRENT_TIMESTAMP, collected_products=?, failed_products=? WHERE id=?",
                    (status, self.collected, self.failed, self.job_id),
                )
            else:
                conn.execute(
                    "UPDATE crawl_jobs SET status=?, started_at=CURRENT_TIMESTAMP WHERE id=?",
                    (status, self.job_id),
                )

    def _update_url_status(self, url_id: int, status: str, collected: int = 0, error: str = None):
        with get_db() as conn:
            conn.execute(
                "UPDATE crawl_job_urls SET status=?, collected_count=?, error_message=? WHERE id=?",
                (status, collected, error, url_id),
            )

    def _broadcast(self, data: dict):
        data["job_id"] = self.job_id
        data["timestamp"] = datetime.now().strftime("%H:%M:%S")
        broadcast_progress(self.job_id, data)

    @staticmethod
    def _get_proxy() -> dict:
        pool = PROXY_POOL.copy()
        random.shuffle(pool)
        return pool[0]

    @staticmethod
    async def _delay(min_sec=None, max_sec=None):
        d = random.uniform(min_sec or 3, max_sec or 8)
        await asyncio.sleep(d)
