"""
naver_commerce_service.py — 네이버 커머스 API (스마트스토어 v2).

bcrypt 토큰 인증, 상품 등록/수정/삭제. (스마트스토어 4/29 customsDutyInfo 필수)
EC2 의존: NAVER_COMMERCE_CLIENT_ID/SECRET .env 필요.
"""
import base64
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

# ── 글로벌 Rate Limiter (적응형) ─────────────────────────────
# 네이버 API 동시 요청수 제한 (이미지 업로드 + 상품 등록 합산)
_API_CONCURRENCY = int(os.environ.get("NAVER_API_CONCURRENCY", "2"))
_API_SEM = threading.Semaphore(_API_CONCURRENCY)
_REQUEST_INTERVAL_SEC = float(os.environ.get("NAVER_REQUEST_INTERVAL", "0.65"))
_LAST_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_TIME = 0.0
# 적응형: 429 발생 시 간격을 자동으로 늘리고, 안정되면 원래로 복귀
_ADAPTIVE_INTERVAL = _REQUEST_INTERVAL_SEC
_ADAPTIVE_LOCK = threading.Lock()
_CONSECUTIVE_OK = 0  # 연속 성공 횟수

# ── 재시도 정책 ────────────────────────────────────────────────
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 6
_BASE_BACKOFF_SEC = 2.0


def _on_429():
    """429 발생 시 요청 간격을 자동으로 늘림."""
    global _ADAPTIVE_INTERVAL, _CONSECUTIVE_OK
    with _ADAPTIVE_LOCK:
        _ADAPTIVE_INTERVAL = min(_ADAPTIVE_INTERVAL + 0.15, 1.0)
        _CONSECUTIVE_OK = 0
        logger.info(f"[adaptive-throttle] 429 감지 → 간격 {_ADAPTIVE_INTERVAL:.2f}s")


def _on_success():
    """성공 시 연속 50회 이상이면 간격을 서서히 줄임."""
    global _ADAPTIVE_INTERVAL, _CONSECUTIVE_OK
    with _ADAPTIVE_LOCK:
        _CONSECUTIVE_OK += 1
        if _CONSECUTIVE_OK >= 50 and _ADAPTIVE_INTERVAL > _REQUEST_INTERVAL_SEC:
            _ADAPTIVE_INTERVAL = max(_ADAPTIVE_INTERVAL - 0.05, _REQUEST_INTERVAL_SEC)
            _CONSECUTIVE_OK = 0
            logger.info(f"[adaptive-throttle] 안정 → 간격 {_ADAPTIVE_INTERVAL:.2f}s")


def _throttle():
    """최소 요청 간격 보장 (적응형)."""
    global _LAST_REQUEST_TIME
    with _LAST_REQUEST_LOCK:
        now = time.time()
        elapsed = now - _LAST_REQUEST_TIME
        if elapsed < _ADAPTIVE_INTERVAL:
            time.sleep(_ADAPTIVE_INTERVAL - elapsed)
        _LAST_REQUEST_TIME = time.time()


def _request_with_retry(method: str, url: str, **kwargs) -> Optional[requests.Response]:
    """429/5xx 시 지수 백오프 재시도. 글로벌 세마포어 + 쓰로틀링 적용."""
    for attempt in range(_MAX_RETRIES + 1):
        with _API_SEM:
            _throttle()
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
            if r.status_code == 429:
                _on_429()
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
        _on_success()
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


def register_product(payload: dict) -> Optional[dict]:
    """상품 등록 (POST /v2/products). 스킵 대상 에러 시 {"_skip": reason} 반환."""
    token = _get_token()
    if not token:
        return None
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
        if r.status_code >= 400:
            body = r.json() if r.text else {}
            inputs = body.get("invalidInputs") or []
            skip_reasons = [i["message"] for i in inputs if i.get("type") in SKIP_ERROR_TYPES]
            if skip_reasons:
                reason = skip_reasons[0]
                logger.warning(f"네이버 등록 스킵 (카테고리 제한): {reason}")
                return {"_skip": reason}
            logger.error(f"네이버 상품 등록 실패: {r.status_code} {r.text[:200]}")
            return None
        return r.json()
    except Exception as e:
        logger.error(f"네이버 등록 예외: {e}")
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
