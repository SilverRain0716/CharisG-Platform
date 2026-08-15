"""
proxy_pool.py — Webshare 로테이팅 US 프록시 풀 (10 IP).

DS 크롤러(Amazon)에서 사용. 셀러 계정 IP와 절대 분리.
.env: PROXY_HOST, PROXY_PORT, PROXY_USER_BASE, PROXY_PASSWORD
"""
import os
import random
import threading
from typing import Optional

from backend_shared._config import (
    PROXY_HOST,
    PROXY_PORT,
    PROXY_USER_BASE,
    PROXY_PASSWORD,
)


class ProxyPool:
    """Webshare 로테이팅 US 프록시 풀 — 10 IP 라운드로빈/랜덤."""

    def __init__(self, ip_count: int = 10):
        self._ip_count = ip_count
        self._idx = 0
        self._lock = threading.Lock()

    def get(self, mode: str = "random") -> Optional[dict]:
        """
        Returns: requests.proxies 형식 dict
            {"http": "http://user-N:pass@host:port", "https": "..."}
        프록시 미설정 시 None
        """
        if not (PROXY_HOST and PROXY_PORT and PROXY_USER_BASE and PROXY_PASSWORD):
            return None

        with self._lock:
            if mode == "round_robin":
                ip_num = self._idx + 1
                self._idx = (self._idx + 1) % self._ip_count
            else:
                ip_num = random.randint(1, self._ip_count)

        user = f"{PROXY_USER_BASE}-{ip_num}"
        url = f"http://{user}:{PROXY_PASSWORD}@{PROXY_HOST}:{PROXY_PORT}"
        return {"http": url, "https": url}

    def get_url(self, mode: str = "random") -> Optional[str]:
        p = self.get(mode)
        return p["http"] if p else None


_default_pool: Optional[ProxyPool] = None


def get_default_pool() -> ProxyPool:
    global _default_pool
    if _default_pool is None:
        _default_pool = ProxyPool()
    return _default_pool
