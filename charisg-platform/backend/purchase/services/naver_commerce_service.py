"""
naver_commerce_service.py — 네이버 커머스 API (스마트스토어 v2).

bcrypt 토큰 인증, 상품 등록/수정/삭제. (스마트스토어 4/29 customsDutyInfo 필수)
EC2 의존: NAVER_COMMERCE_CLIENT_ID/SECRET .env 필요.
"""
import base64
import copy
import logging
import os
import threading
import time
from typing import Optional

import bcrypt
import requests
from requests.adapters import HTTPAdapter

from backend_shared._config import NAVER_COMMERCE_CLIENT_ID, NAVER_COMMERCE_CLIENT_SECRET

logger = logging.getLogger(__name__)

BASE = "https://api.commerce.naver.com/external"

# ── HTTP Session (Connection Pool) ────────────────────────────
_SESSION = requests.Session()
_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20)
_SESSION.mount("https://", _adapter)
_SESSION.mount("http://", _adapter)

# ── Token Cache ───────────────────────────────────────────────
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: dict = {"access_token": None, "expires_at": 0.0}
_TOKEN_SAFETY_MARGIN_SEC = 120

# ── 글로벌 Rate Limiter (네이버 공식 2/s, 직렬 큐) ────────────
# 네이버 커머스 API 공식 제한: 초당 2회 (내스토어 앱 기준)
# 모든 API 호출을 단일 Lock으로 직렬화하여 0.55초 간격(≈1.8/s) 보장
_GATE_LOCK = threading.Lock()
_LAST_REQUEST_TIME = 0.0
_MIN_INTERVAL = 0.55  # 초당 ~1.8회 (2/s 미만으로 안전 마진)

# ── 재시도 정책 ────────────────────────────────────────────────
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 6
_BASE_BACKOFF_SEC = 2.0


def _gate():
    """모든 네이버 API 호출 전 반드시 통과. 최소 간격 보장 (직렬)."""
    global _LAST_REQUEST_TIME
    with _GATE_LOCK:
        now = time.time()
        elapsed = now - _LAST_REQUEST_TIME
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        _LAST_REQUEST_TIME = time.time()


def _request_with_retry(method: str, url: str, **kwargs) -> Optional[requests.Response]:
    """429/5xx 시 지수 백오프 재시도. 글로벌 직렬 gate로 2/s 이내 보장."""
    for attempt in range(_MAX_RETRIES + 1):
        _gate()
        try:
            r = _SESSION.request(method, url, **kwargs)
        except requests.exceptions.RequestException as e:
            if attempt >= _MAX_RETRIES:
                logger.warning(f"네이버 네트워크 예외 (끝까지): {e}")
                return None
            wait = _BASE_BACKOFF_SEC * (2 ** attempt)
            logger.warning(f"네이버 네트워크 예외 → {wait:.1f}s 대기 후 재시도 ({attempt + 1}/{_MAX_RETRIES}): {e}")
            time.sleep(wait)
            continue

        if r.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
            retry_after = r.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else _BASE_BACKOFF_SEC * (2 ** attempt)
            except ValueError:
                wait = _BASE_BACKOFF_SEC * (2 ** attempt)
            wait = min(wait, 30.0)
            logger.warning(
                f"네이버 {r.status_code} 재시도 — {wait:.1f}s 대기 ({attempt + 1}/{_MAX_RETRIES}) {url[-60:]}"
            )
            time.sleep(wait)
            continue
        return r
    return None


def _issue_new_token() -> Optional[tuple[str, float]]:
    ts = int((time.time() - 3) * 1000)
    msg = f"{NAVER_COMMERCE_CLIENT_ID}_{ts}"
    salt = NAVER_COMMERCE_CLIENT_SECRET.encode()
    hashed = bcrypt.hashpw(msg.encode(), salt)
    sig_b64 = base64.standard_b64encode(hashed).decode()

    r = _request_with_retry(
        "POST",
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
    if r is None or r.status_code >= 400:
        logger.error(f"네이버 커머스 토큰 발급 실패: {r.status_code if r is not None else 'no-response'} {r.text[:200] if r is not None else ''}")
        return None
    data = r.json()
    tok = data.get("access_token")
    expires_in = int(data.get("expires_in", 3600))
    if not tok:
        return None
    return tok, time.time() + expires_in


def _get_token() -> Optional[str]:
    """캐시된 토큰 반환 — 만료 전 재사용, 만료 임박 시 재발급."""
    if not (NAVER_COMMERCE_CLIENT_ID and NAVER_COMMERCE_CLIENT_SECRET):
        logger.warning("NAVER_COMMERCE_CLIENT_* 미설정")
        return None

    now = time.time()
    cached = _TOKEN_CACHE.get("access_token")
    expires_at = _TOKEN_CACHE.get("expires_at", 0.0)
    if cached and now < expires_at - _TOKEN_SAFETY_MARGIN_SEC:
        return cached

    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get("access_token")
        expires_at = _TOKEN_CACHE.get("expires_at", 0.0)
        if cached and time.time() < expires_at - _TOKEN_SAFETY_MARGIN_SEC:
            return cached
        issued = _issue_new_token()
        if not issued:
            return None
        tok, exp = issued
        _TOKEN_CACHE["access_token"] = tok
        _TOKEN_CACHE["expires_at"] = exp
        return tok


def upload_images_batch(file_paths: list[str]) -> list[Optional[str]]:
    """여러 이미지를 한 번의 API 호출로 업로드 (최대 10개). 호출 1회로 10장 처리."""
    token = _get_token()
    if not token:
        return [None] * len(file_paths)

    import mimetypes
    files_data = []
    valid_indices: list[int] = []

    for i, path in enumerate(file_paths):
        mime = mimetypes.guess_type(path)[0] or "image/jpeg"
        try:
            with open(path, "rb") as f:
                data = f.read()
            if not data:
                logger.warning(f"빈 이미지 파일: {path}")
                continue
            files_data.append(("imageFiles", (path.split("/")[-1], data, mime)))
            valid_indices.append(i)
        except Exception as e:
            logger.warning(f"이미지 읽기 실패 {path}: {e}")

    if not files_data:
        return [None] * len(file_paths)

    r = _request_with_retry(
        "POST",
        BASE + "/v1/product-images/upload",
        files=files_data,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if r is None or r.status_code >= 400:
        logger.error(
            f"네이버 이미지 배치 업로드 실패: "
            f"{r.status_code if r else 'no-response'} "
            f"{r.text[:200] if r else ''}"
        )
        return [None] * len(file_paths)

    resp = r.json()
    images = resp.get("images") or []

    results: list[Optional[str]] = [None] * len(file_paths)
    for idx, img in zip(valid_indices, images):
        url = img.get("url")
        if url:
            results[idx] = url

    logger.info(f"네이버 이미지 배치 업로드 완료: {sum(1 for u in results if u)}/{len(file_paths)}장")
    return results


def upload_image(file_path: str) -> Optional[str]:
    """이미지 파일을 네이버에 업로드 후 URL 반환."""
    token = _get_token()
    if not token:
        return None
    try:
        import mimetypes
        mime = mimetypes.guess_type(file_path)[0] or "image/jpeg"
        # NOTE: 재시도 시 파일 스트림 소진 방지 — bytes 로 선읽기
        with open(file_path, "rb") as f:
            data = f.read()
        if not data:
            logger.error(f"네이버 이미지 업로드: 빈 파일 {file_path}")
            return None
        r = _request_with_retry(
            "POST",
            BASE + "/v1/product-images/upload",
            files={"imageFiles": (file_path.split("/")[-1], data, mime)},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if r is None or r.status_code >= 400:
            logger.error(f"네이버 이미지 업로드 실패: {r.status_code if r is not None else 'no-response'} {r.text[:200] if r is not None else ''}")
            return None
        data = r.json()
        images = data.get("images") or []
        if images:
            return images[0].get("url")
        return None
    except Exception as e:
        logger.error(f"네이버 이미지 업로드 예외: {e}")
        return None


SKIP_ERROR_TYPES = {
    "NotAuthority.product.category.id",
    "Empty.product.detailAttribute.certificationInfos.kindType",
    "book.CheckValidationIsbn13",
}


def _extract_restricted_words(response_body: dict) -> set[str]:
    """네이버 에러 응답에서 태그 등록불가 단어를 추출."""
    import re as _re
    words = set()
    for inp in response_body.get("invalidInputs") or []:
        msg = inp.get("message", "")
        m = _re.search(r"등록불가인 단어\(([^)]+)\)", msg)
        if m:
            for w in m.group(1).split(","):
                words.add(w.strip())
    return words


def register_product(payload: dict) -> Optional[dict]:
    """상품 등록 (POST /v2/products). 태그 금지어 자동 재시도 + 스킵 대상 에러 처리.

    재시도 중 payload를 직접 변경하므로 호출자 dict 보호를 위해 deepcopy한다.
    """
    token = _get_token()
    if not token:
        return None

    payload = copy.deepcopy(payload)
    for tag_attempt in range(4):
        try:
            r = _request_with_retry(
                "POST",
                BASE + "/v2/products",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if r is None:
                return None
            if r.status_code < 400:
                return r.json()

            body = r.json() if r.text else {}
            inputs = body.get("invalidInputs") or []

            # 스킵 대상 에러 (카테고리 제한 등)
            skip_reasons = [i["message"] for i in inputs if i.get("type") in SKIP_ERROR_TYPES]
            if skip_reasons:
                reason = skip_reasons[0]
                logger.warning(f"네이버 등록 스킵 (카테고리 제한): {reason}")
                return {"_skip": reason}

            # 태그 금지어 에러 → 금지어 제거 후 재시도
            restricted = _extract_restricted_words(body)
            da = payload.get("originProduct", {}).get("detailAttribute", {})
            seo = da.get("seoInfo", {})
            tags = seo.get("sellerTags", [])

            if restricted and tags:
                filtered = [t for t in tags if t["text"] not in restricted]
                if filtered:
                    seo["sellerTags"] = filtered
                    logger.info(f"태그 금지어 {restricted} 제거 → {len(filtered)}개로 재시도")
                    continue
                else:
                    # 태그 전부 금지 → seoInfo 제거 후 재시도
                    da.pop("seoInfo", None)
                    logger.info("태그 전부 금지 → 태그 없이 재시도")
                    continue

            logger.error(f"네이버 상품 등록 실패: {r.status_code} {r.text[:200]}")
            return None
        except Exception as e:
            logger.error(f"네이버 등록 예외: {e}")
            return None

    logger.error("네이버 등록 태그 재시도 초과")
    return None


def get_product(product_no: str) -> Optional[dict]:
    """상품 조회 (GET /v2/products/origin-products/{productNo})."""
    token = _get_token()
    if not token:
        return None
    try:
        r = _request_with_retry(
            "GET",
            BASE + f"/v2/products/origin-products/{product_no}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if r is None or r.status_code >= 400:
            return None
        return r.json()
    except Exception:
        return None


def get_addressbooks(page: int = 1, size: int = 20) -> Optional[dict]:
    """셀러 주소록 조회 (출고지 RELEASE / 반품지 REFUND_OR_EXCHANGE / GENERAL).

    응답 예: {"addressBooks": [{"addressBookNo": ..., "addressType": "RELEASE",
                                "postalCode": ..., "baseAddress": ..., "detailAddress": ...,
                                "phoneNumber1": ..., "phoneNumber2": ...,
                                "roadNameAddress": bool, "overseasAddress": bool}, ...],
             "page": 1, "totalPage": N}

    권한: [판매자정보] 그룹 필수. 미부여 시 GW.AUTHN.
    """
    token = _get_token()
    if not token:
        return None
    try:
        r = _request_with_retry(
            "GET",
            BASE + f"/v1/seller/addressbooks-for-page?page={page}&size={size}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if r is None or r.status_code >= 400:
            logger.error(f"네이버 주소록 조회 실패: {r.status_code if r is not None else 'no-response'} {r.text[:200] if r is not None else ''}")
            return None
        return r.json()
    except Exception as e:
        logger.error(f"네이버 주소록 조회 예외: {e}")
        return None


def get_addressbook_by_type(address_type: str) -> Optional[dict]:
    """RELEASE/REFUND_OR_EXCHANGE 중 첫 번째 매칭 항목 반환. 페이지를 끝까지 순회."""
    page = 1
    while True:
        result = get_addressbooks(page=page)
        if not result:
            return None
        for entry in result.get("addressBooks", []):
            if entry.get("addressType") == address_type:
                return entry
        if page >= result.get("totalPage", 1):
            return None
        page += 1


def update_product(product_no: str, partial: dict) -> Optional[dict]:
    """상품 수정 — GET으로 전체 데이터 가져온 뒤 partial 병합 후 PUT."""
    token = _get_token()
    if not token:
        return None
    current = get_product(product_no)
    if not current:
        logger.error(f"네이버 상품 조회 실패 (수정 전): {product_no}")
        return None
    for key, val in partial.get("originProduct", {}).items():
        current["originProduct"][key] = val
    try:
        r = _request_with_retry(
            "PUT",
            BASE + f"/v2/products/origin-products/{product_no}",
            json=current,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if r is None or r.status_code >= 400:
            logger.error(f"네이버 상품 수정 실패: {r.status_code if r is not None else 'no-response'} {r.text[:200] if r is not None else ''}")
            return None
        return r.json()
    except Exception as e:
        logger.error(f"네이버 수정 예외: {e}")
        return None
