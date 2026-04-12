"""
naver_commerce_service.py — 네이버 커머스 API (스마트스토어 v2).

bcrypt 토큰 인증, 상품 등록/수정/삭제. (스마트스토어 4/29 customsDutyInfo 필수)
EC2 의존: NAVER_COMMERCE_CLIENT_ID/SECRET .env 필요.
"""
import base64
import logging
import time
from typing import Optional

import bcrypt
import requests

from backend_shared._config import NAVER_COMMERCE_CLIENT_ID, NAVER_COMMERCE_CLIENT_SECRET

logger = logging.getLogger(__name__)

BASE = "https://api.commerce.naver.com/external"


def _get_token() -> Optional[str]:
    """네이버 커머스 OAuth — bcrypt 서명 + base64 인코딩.

    Note: timestamp 는 현재시각 - 3초 (서버 시각 오차 보정).
    """
    if not (NAVER_COMMERCE_CLIENT_ID and NAVER_COMMERCE_CLIENT_SECRET):
        logger.warning("NAVER_COMMERCE_CLIENT_* 미설정")
        return None

    ts = int((time.time() - 3) * 1000)
    msg = f"{NAVER_COMMERCE_CLIENT_ID}_{ts}"
    salt = NAVER_COMMERCE_CLIENT_SECRET.encode()
    hashed = bcrypt.hashpw(msg.encode(), salt)
    sig_b64 = base64.standard_b64encode(hashed).decode()

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


def upload_image(file_path: str) -> Optional[str]:
    """이미지 파일을 네이버에 업로드 후 URL 반환."""
    token = _get_token()
    if not token:
        return None
    try:
        import mimetypes
        mime = mimetypes.guess_type(file_path)[0] or "image/jpeg"
        with open(file_path, "rb") as f:
            r = requests.post(
                BASE + "/v1/product-images/upload",
                files={"imageFiles": (file_path.split("/")[-1], f, mime)},
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
        if r.status_code >= 400:
            logger.error(f"네이버 이미지 업로드 실패: {r.status_code} {r.text[:200]}")
            return None
        data = r.json()
        images = data.get("images") or []
        if images:
            return images[0].get("url")
        return None
    except Exception as e:
        logger.error(f"네이버 이미지 업로드 예외: {e}")
        return None


def register_product(payload: dict) -> Optional[dict]:
    """상품 등록 (POST /v1/products)."""
    token = _get_token()
    if not token:
        return None
    try:
        r = requests.post(
            BASE + "/v2/products",
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
