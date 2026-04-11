"""
coupang_service.py — 쿠팡 WING API.

HMAC-SHA256 서명. 상품 등록/수정/주문 조회.
EC2 의존: COUPANG_ACCESS_KEY/SECRET_KEY/VENDOR_ID
"""
import hashlib
import hmac
import json
import logging
import time
from typing import Optional
from urllib.parse import urlparse

import requests

from backend_shared._config import COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY, COUPANG_VENDOR_ID

logger = logging.getLogger(__name__)

BASE = "https://api-gateway.coupang.com"


def _signature(method: str, path: str, query: str = "") -> dict:
    ts = time.strftime("%y%m%dT%H%M%SZ", time.gmtime())
    message = ts + method + path + query
    sig = hmac.new(
        COUPANG_SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Authorization": f"CEA algorithm=HmacSHA256, access-key={COUPANG_ACCESS_KEY}, signed-date={ts}, signature={sig}",
        "Content-Type": "application/json",
    }


def register_product(payload: dict) -> Optional[dict]:
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_VENDOR_ID):
        logger.warning("COUPANG_* 미설정")
        return None
    path = f"/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
    try:
        r = requests.post(
            BASE + path,
            headers=_signature("POST", path),
            json=payload,
            timeout=15,
        )
        if r.status_code >= 400:
            logger.error(f"쿠팡 상품 등록 실패: {r.status_code} {r.text[:200]}")
        return r.json()
    except Exception as e:
        logger.error(f"쿠팡 등록 예외: {e}")
        return None


def get_orders(start: str, end: str) -> Optional[list]:
    path = f"/v2/providers/openapi/apis/api/v4/vendors/{COUPANG_VENDOR_ID}/ordersheets"
    query = f"createdAtFrom={start}&createdAtTo={end}&status=ACCEPT"
    try:
        r = requests.get(
            BASE + path + "?" + query,
            headers=_signature("GET", path, query),
            timeout=15,
        )
        return r.json().get("data", [])
    except Exception as e:
        logger.error(f"쿠팡 주문 조회 실패: {e}")
        return None
