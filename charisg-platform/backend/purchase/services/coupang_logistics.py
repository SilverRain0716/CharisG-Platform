"""
coupang_logistics.py — 쿠팡 출고지/반품지 등록·조회·수정.

Naver addressbook → Coupang 페이로드 매퍼 + GET/POST/PUT wrapper.
1회 셋업: scripts/setup_coupang_logistics.py에서 호출.

쿠팡 WING API 참조:
- POST /v2/providers/openapi/apis/api/v4/vendors/{vendorId}/outboundShippingCenters
- POST /v2/providers/openapi/apis/api/v4/vendors/{vendorId}/returnShippingCenters
- GET  /v2/providers/marketplace_openapi/apis/api/v1/vendor/shipping-place/outbound
- GET  /v2/providers/openapi/apis/api/v4/vendors/{vendorId}/returnShippingCenters
"""
import json
import logging
import re
from typing import Optional

import requests

from backend.purchase.services.coupang_service import _signature, BASE
from backend.purchase.services import policy_constants as P
from backend_shared._config import COUPANG_VENDOR_ID

logger = logging.getLogger(__name__)


# ── Naver address → Coupang country code 변환 ──────────────────

# 네이버 address 문자열 끝에 국가명이 영문/한글로 노출됨 (예: "..., New Jersey, United States")
_COUNTRY_PATTERNS = {
    "US": [r"\bUnited States\b", r"\bUSA\b", r"\b미국\b"],
    "CN": [r"\bChina\b", r"\b중국\b"],
    "KR": [r"\bKorea\b", r"\b대한민국\b", r"\b한국\b"],
    "JP": [r"\bJapan\b", r"\b일본\b"],
}


def _detect_country_code(naver_entry: dict) -> str:
    """address 풀 문자열 + overseasAddress 플래그로 ISO 2-letter 국가 코드 추정.

    국내 → KR, 매칭 실패 + overseas → 호출자가 보정 필요.
    """
    if not naver_entry.get("overseasAddress"):
        return "KR"
    address = naver_entry.get("address", "") or ""
    for code, patterns in _COUNTRY_PATTERNS.items():
        for p in patterns:
            if re.search(p, address, re.IGNORECASE):
                return code
    logger.warning(f"국가 코드 추정 실패 — address={address!r}, 기본 US 사용")
    return "US"


def _split_phone(phone: str) -> tuple[str, str]:
    """phoneNumber 문자열에서 (companyContactNumber, phoneNumber2) 분리.

    쿠팡은 두 필드를 분리 — 회사 대표번호 + 담당자 휴대폰.
    네이버는 phoneNumber1/phoneNumber2를 그대로 전달.
    """
    return (phone or "").strip(), ""


# ── 매퍼: Naver entry → Coupang payload ──────────────────────────

def naver_to_coupang_outbound(naver_entry: dict, user_id: str) -> dict:
    """네이버 RELEASE 주소록 → 쿠팡 outboundShippingCenters POST 페이로드."""
    country = _detect_country_code(naver_entry)
    address_type = "ROADNAME" if naver_entry.get("roadNameAddress") else "JIBUN"
    return {
        "vendorId": COUPANG_VENDOR_ID,
        "userId": user_id,
        "shippingPlaceName": naver_entry.get("name", "출고지")[:50],
        "placeAddresses": [
            {
                "addressType": address_type,
                "countryCode": country,
                "companyContactNumber": naver_entry.get("phoneNumber1", "") or P.AS_PHONE,
                "phoneNumber2": naver_entry.get("phoneNumber2", "") or "",
                "returnZipCode": naver_entry.get("postalCode", ""),
                "returnAddress": naver_entry.get("baseAddress", ""),
                "returnAddressDetail": naver_entry.get("detailAddress", ""),
            }
        ],
        "remoteInfos": [
            {
                "deliveryCode": P.DELIVERY_COMPANY_COUPANG,
                "usable": False,           # 도서산간 추가배송 미사용
                "jejuShippingFee": 0,
                "notJejuShippingFee": 0,
            }
        ],
    }


def naver_to_coupang_return(naver_entry: dict, user_id: str) -> dict:
    """네이버 REFUND_OR_EXCHANGE 주소록 → 쿠팡 returnShippingCenters POST 페이로드."""
    country = _detect_country_code(naver_entry)
    address_type = "ROADNAME" if naver_entry.get("roadNameAddress") else "JIBUN"
    return {
        "vendorId": COUPANG_VENDOR_ID,
        "userId": user_id,
        "shippingPlaceName": naver_entry.get("name", "반품지")[:50],
        "deliverCode": P.DELIVERY_COMPANY_COUPANG,
        "placeAddresses": [
            {
                "addressType": address_type,
                "countryCode": country,
                "companyContactNumber": naver_entry.get("phoneNumber1", "") or P.AS_PHONE,
                "phoneNumber2": naver_entry.get("phoneNumber2", "") or "",
                "returnZipCode": naver_entry.get("postalCode", ""),
                "returnAddress": naver_entry.get("baseAddress", ""),
                "returnAddressDetail": naver_entry.get("detailAddress", ""),
            }
        ],
        "goodsflowInfoOpenApiDto": None,   # 굿스플로 미연동
    }


# ── 쿠팡 API wrapper ──────────────────────────────────────────────

def list_outbound_shipping_centers(page: int = 1, size: int = 10) -> Optional[dict]:
    """출고지 목록 GET."""
    path = "/v2/providers/marketplace_openapi/apis/api/v1/vendor/shipping-place/outbound"
    query = f"pageNum={page}&pageSize={size}"
    try:
        r = requests.get(BASE + path + "?" + query, headers=_signature("GET", path, query), timeout=15)
        if r.status_code >= 400:
            logger.error(f"쿠팡 출고지 조회 실패: {r.status_code} {r.text[:200]}")
            return None
        return r.json()
    except Exception as e:
        logger.error(f"쿠팡 출고지 조회 예외: {e}")
        return None


def list_return_shipping_centers(page: int = 1, size: int = 10) -> Optional[dict]:
    """반품지 목록 GET."""
    path = f"/v2/providers/openapi/apis/api/v4/vendors/{COUPANG_VENDOR_ID}/returnShippingCenters"
    query = f"pageNum={page}&pageSize={size}"
    try:
        r = requests.get(BASE + path + "?" + query, headers=_signature("GET", path, query), timeout=15)
        if r.status_code >= 400:
            logger.error(f"쿠팡 반품지 조회 실패: {r.status_code} {r.text[:200]}")
            return None
        return r.json()
    except Exception as e:
        logger.error(f"쿠팡 반품지 조회 예외: {e}")
        return None


def create_outbound_shipping_center(payload: dict) -> Optional[dict]:
    """출고지 신규 등록 POST. 응답 data.outboundShippingPlaceCode 발급."""
    path = f"/v2/providers/openapi/apis/api/v4/vendors/{COUPANG_VENDOR_ID}/outboundShippingCenters"
    try:
        r = requests.post(
            BASE + path,
            headers=_signature("POST", path),
            json=payload,
            timeout=15,
        )
        if r.status_code >= 400:
            logger.error(f"쿠팡 출고지 등록 실패: {r.status_code} {r.text[:300]}")
            return None
        return r.json()
    except Exception as e:
        logger.error(f"쿠팡 출고지 등록 예외: {e}")
        return None


def create_return_shipping_center(payload: dict) -> Optional[dict]:
    """반품지 신규 등록 POST. 응답 data.returnCenterCode 발급."""
    path = f"/v2/providers/openapi/apis/api/v4/vendors/{COUPANG_VENDOR_ID}/returnShippingCenters"
    try:
        r = requests.post(
            BASE + path,
            headers=_signature("POST", path),
            json=payload,
            timeout=15,
        )
        if r.status_code >= 400:
            logger.error(f"쿠팡 반품지 등록 실패: {r.status_code} {r.text[:300]}")
            return None
        return r.json()
    except Exception as e:
        logger.error(f"쿠팡 반품지 등록 예외: {e}")
        return None


def update_outbound_shipping_center(code: str, payload: dict) -> Optional[dict]:
    path = f"/v2/providers/openapi/apis/api/v4/vendors/{COUPANG_VENDOR_ID}/outboundShippingCenters/{code}"
    try:
        r = requests.put(BASE + path, headers=_signature("PUT", path), json=payload, timeout=15)
        if r.status_code >= 400:
            logger.error(f"쿠팡 출고지 수정 실패: {r.status_code} {r.text[:300]}")
            return None
        return r.json()
    except Exception as e:
        logger.error(f"쿠팡 출고지 수정 예외: {e}")
        return None


def update_return_shipping_center(code: str, payload: dict) -> Optional[dict]:
    path = f"/v2/providers/openapi/apis/api/v4/vendors/{COUPANG_VENDOR_ID}/returnShippingCenters/{code}"
    try:
        r = requests.put(BASE + path, headers=_signature("PUT", path), json=payload, timeout=15)
        if r.status_code >= 400:
            logger.error(f"쿠팡 반품지 수정 실패: {r.status_code} {r.text[:300]}")
            return None
        return r.json()
    except Exception as e:
        logger.error(f"쿠팡 반품지 수정 예외: {e}")
        return None
