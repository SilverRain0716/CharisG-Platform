"""
rate_limiter.py — 범용 토큰 버킷/슬라이딩 윈도우 레이트 리미터.

용도:
- 외부 API 호출 속도 제한 (네이버 데이터랩, Google Trends 등)
- 크롤러 요청 간 딜레이
- 백오프 정책
"""
import logging
import random
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


class RateLimiter:
    """슬라이딩 윈도우 기반 RPM 리미터."""

    def __init__(self, max_per_minute: int = 30, name: str = "default"):
        self._max = max_per_minute
        self._name = name
        self._calls: list[float] = []
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.time()
            self._calls = [t for t in self._calls if now - t < 60]
            if len(self._calls) >= self._max:
                oldest = self._calls[0]
                wait_sec = 60 - (now - oldest) + 0.5
                if wait_sec > 0:
                    logger.info(f"⏳ [{self._name}] RPM {self._max} 초과 → {wait_sec:.1f}초 대기")
                    time.sleep(wait_sec)
            self._calls.append(time.time())


class CrawlerDelayer:
    """크롤러 요청 간 랜덤 딜레이 + 쿨다운 + 실패 백오프.

    DS amazon_keyword_crawler 차단 방지 정책:
    - 요청 간 15-25초 랜덤
    - 50 키워드마다 2-3분 쿨다운
    - CAPTCHA 감지 시 5분 대기
    - 연속 실패 3회 → 10분, 5회 → 중단
    """

    def __init__(
        self,
        delay_min: float = 15.0,
        delay_max: float = 25.0,
        cooldown_every: int = 50,
        cooldown_min: float = 120.0,
        cooldown_max: float = 180.0,
        captcha_wait: float = 300.0,
        max_consecutive_failures: int = 5,
        soft_failure_threshold: int = 3,
        soft_failure_wait: float = 600.0,
        name: str = "amazon_crawler",
    ):
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.cooldown_every = cooldown_every
        self.cooldown_min = cooldown_min
        self.cooldown_max = cooldown_max
        self.captcha_wait = captcha_wait
        self.max_failures = max_consecutive_failures
        self.soft_failures = soft_failure_threshold
        self.soft_failure_wait = soft_failure_wait
        self.name = name

        self._counter = 0
        self._consecutive_failures = 0
        self._aborted = False

    def before_request(self) -> bool:
        """요청 전 호출. False 반환 시 크롤링 중단해야 함."""
        if self._aborted:
            return False

        if self._counter > 0:
            delay = random.uniform(self.delay_min, self.delay_max)
            time.sleep(delay)

        if self._counter > 0 and self._counter % self.cooldown_every == 0:
            cool = random.uniform(self.cooldown_min, self.cooldown_max)
            logger.info(f"⏸ [{self.name}] {self._counter} 요청 처리 → {cool:.0f}초 쿨다운")
            time.sleep(cool)

        self._counter += 1
        return True

    def report_success(self) -> None:
        self._consecutive_failures = 0

    def report_failure(self, captcha: bool = False) -> None:
        self._consecutive_failures += 1

        if captcha:
            logger.warning(f"⚠ [{self.name}] CAPTCHA 감지 → {self.captcha_wait:.0f}초 대기")
            time.sleep(self.captcha_wait)
            return

        if self._consecutive_failures >= self.max_failures:
            logger.error(f"⛔ [{self.name}] 연속 실패 {self.max_failures}회 → 크롤링 중단")
            self._aborted = True
            return

        if self._consecutive_failures >= self.soft_failures:
            logger.warning(
                f"⚠ [{self.name}] 연속 실패 {self._consecutive_failures}회 "
                f"→ {self.soft_failure_wait:.0f}초 정지"
            )
            time.sleep(self.soft_failure_wait)

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def request_count(self) -> int:
        return self._counter
