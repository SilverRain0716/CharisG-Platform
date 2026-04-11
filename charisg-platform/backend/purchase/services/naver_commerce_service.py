"""
naver_commerce_service.py — 네이버 커머스 API (스마트스토어 v2).

bcrypt 토큰 인증, 상품 등록/수정/삭제. (스마트스토어 4/29 customsDutyInfo 필수)
EC2 의존: NAVER_COMMERCE_CLIENT_ID/SECRET .env 필요.
"""
import logging
import time
from typing import Optional

import bcrypt
import requests

from backend_shared._config import NAVER_COMMERCE_CLIENT_ID, NAVER_COMMERCE_CLIENT_SECRET

logger = logging.getLogger(__name__)

BASE = "https://api.commerce.naver.com/external"


def _get_token() -> Optional[str]:
    """네이버 커머스 OAuth — bcrypt 서명 기반.

    Note: timestamp 는 현재시각 - 3초 (서버 시각 오차 보정).
    """
    if not (NAVER_COMMERCE_CLIENT_ID and NAVER_COMMERCE_CLIENT_SECRET):
        logger.warning("NAVER_COMMERCE_CLIENT_* 미설정")
        return None

    ts = int((time.time() - 3) * 1000)
    msg = f"{NAVER_COMMERCE_CLIENT_ID}_{ts}"
    salt = NAVER_COMMERCE_CLIENT_SECRET.encode()
    sig = bcrypt.hashpw(msg.encode(), salt).decode()
    sig_b64 = sig  # 네이버 사양: bcrypt 결과 그대로 base64

    try:
        r = requests.post(
            BASE + "/v1/oauth2/token",
            data={
                "client_id": NAVER_COMMERCE_CLIENT_ID,
                "timestamp": ts,
                "client_secret_sign": sig_b64,
                "grant_type": "client_credentials",
                "type": "SELF",
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception as e:
        logger.error(f"네이버 커머스 토큰 발급 실패: {e}")
        return None


def register_product(payload: dict) -> Optional[dict]:
    """상품 등록 (POST /v1/products)."""
    token = _get_token()
    if not token:
        return None
    try:
        r = requests.post(
            BASE + "/v1/products",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if r.status_code >= 400:
            logger.error(f"네이버 상품 등록 실패: {r.status_code} {r.text[:200]}")
        return r.json()
    except Exception as e:
        logger.error(f"네이버 등록 예외: {e}")
        return None
