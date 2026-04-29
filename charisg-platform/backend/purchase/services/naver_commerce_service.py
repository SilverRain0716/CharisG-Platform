"""
naver_commerce_service.py — 네이버 커머스 API (스마트스토어 v2).

bcrypt 토큰 인증, 상품 등록/수정/삭제. (스마트스토어 4/29 customsDutyInfo 필수)
EC2 의존: NAVER_COMMERCE_CLIENT_ID/SECRET .env 필요.
"""
import base64
import copy
import json
import logging
import os
import threading
import time
from typing import Optional

import bcrypt
import requests
from requests.adapters import HTTPAdapter

from backend_shared._config import NAVER_COMMERCE_CLIENT_ID, NAVER_COMMERCE_CLIENT_SECRET
from backend_shared.utils.rate_limiter import RateLimiter

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

# ── 글로벌 Rate Limiter (초당 + 분당 이중 제한) ────────────
# 네이버 커머스 API:
#   - 공식 초당 제한: 2/s (내스토어 앱 기준) → _MIN_INTERVAL 0.55s로 ≈1.8/s
#   - 비공식 분/시간 단위 누적 쿼터 존재 (특히 /product-images/upload)
#     → 초당만 막으면 4분간 호출이 많아 분단위 쿼터 고갈 후 429 폭탄
# 이중 게이트: 초당(threading.Lock + sleep) + 분당(RateLimiter 슬라이딩 윈도우).
# NAVER_COMMERCE_RPM 환경변수로 분당 한도 조절 (기본 80, 2/s×60×0.66 보수)
_GATE_LOCK = threading.Lock()
_LAST_REQUEST_TIME = 0.0
_MIN_INTERVAL = 1.0   # 초당 1회 (공식 2/s 의 절반 — burst 안전 마진)
_RPM_LIMIT = int(os.environ.get("NAVER_COMMERCE_RPM", "50"))
_rpm_limiter = RateLimiter(max_per_minute=_RPM_LIMIT, name="naver_commerce")

# ── 재시도 정책 ────────────────────────────────────────────────
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 6
_BASE_BACKOFF_SEC = 2.0


def _gate():
    """모든 네이버 API 호출 전 반드시 통과.
    1) 분당 RPM 슬라이딩 윈도우 (누적 쿼터 대비)
    2) 초당 최소 간격 직렬 Lock (공식 2/s 대비)
    """
    global _LAST_REQUEST_TIME
    # 1) 분당 한도
    _rpm_limiter.wait()
    # 2) 초당 간격 (lock 안에서 직렬화)
    with _GATE_LOCK:
        now = time.time()
        elapsed = now - _LAST_REQUEST_TIME
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        _LAST_REQUEST_TIME = time.time()


def _parse_int_header(r: requests.Response, name: str) -> Optional[int]:
    raw = r.headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def _apply_gncp_throttle(r: requests.Response) -> None:
    """네이버 응답 헤더(GNCP-GW-RateLimit-*) 기반 선제 속도 조절.

    Remaining==0 이면 현재 1초 창에서 쿼터 소진 — 다음 창까지 대기해 429 예방.
    Remaining==1 이면 임계 직전 — 짧게 대기해 버스트 완충.
    """
    remaining = _parse_int_header(r, "GNCP-GW-RateLimit-Remaining")
    if remaining is None:
        return
    if remaining <= 0:
        time.sleep(1.5)
    elif remaining == 1:
        time.sleep(0.7)
    elif remaining == 2:
        time.sleep(0.3)


def _format_gncp_headers(r: requests.Response) -> str:
    """429 로그용 — 남은 쿼터/버스트 정보를 한 줄로 포맷."""
    parts = []
    for key, label in (
        ("GNCP-GW-RateLimit-Remaining", "rate"),
        ("GNCP-GW-Quota-Remaining", "quota"),
        ("GNCP-GW-RateLimit-Burst-Capacity", "burst"),
    ):
        v = r.headers.get(key)
        if v is not None:
            parts.append(f"{label}={v}")
    return " ".join(parts)


def _request_with_retry(method: str, url: str, **kwargs) -> Optional[requests.Response]:
    """429/5xx 시 지수 백오프 재시도. GNCP 헤더 기반 선제 속도 조절로 429 발생 자체를 감축."""
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

        # 응답 헤더 기반 선제 속도 조절 (429 맞기 전에 미리 sleep)
        _apply_gncp_throttle(r)

        if r.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
            retry_after = r.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else _BASE_BACKOFF_SEC * (2 ** attempt)
            except ValueError:
                wait = _BASE_BACKOFF_SEC * (2 ** attempt)
            wait = min(wait, 300.0)  # Retry-After 최대 5분까지 존중 (네이버 분/시간 쿼터 회복용)
            gncp = _format_gncp_headers(r)
            gncp_part = f" [{gncp}]" if gncp else ""
            logger.warning(
                f"네이버 {r.status_code} 재시도 — {wait:.1f}s 대기 ({attempt + 1}/{_MAX_RETRIES}){gncp_part} {url[-60:]}"
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


def _resize_for_naver(data: bytes, max_side: int = 800, quality: int = 85) -> bytes:
    """네이버 업로드 직전 800px 리사이즈.

    로컬 파일은 원본(1500px) 유지 — 쿠팡은 public_url 로 이 원본을 pull 하므로 품질 손실 없음.
    네이버는 파일을 multipart 로 직접 전송하는 경로라 이 함수에서만 축소됨.
    이미 800 이하이거나 리사이즈 실패 시 원본 바이트 그대로 반환.
    """
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data))
        if max(img.size) <= max_side:
            return data
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality, optimize=True)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"이미지 리사이즈 실패 — 원본 사용: {e}")
        return data


def upload_images_batch(file_paths: list[str]) -> list[Optional[str]]:
    """여러 이미지를 한 번의 API 호출로 업로드 (최대 10개). 호출 1회로 10장 처리.
    업로드 직전 800px 리사이즈로 페이로드 크기 감소 → Phase 1 속도 개선."""
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
            data = _resize_for_naver(data)
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
    """이미지 파일을 네이버에 업로드 후 URL 반환. 업로드 직전 800px 리사이즈."""
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
        data = _resize_for_naver(data)
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
            # 400 invalidInputs 를 상세 메시지로 요약해 호출자에 전달 (listings_pa.error_message 저장용)
            if inputs:
                summary = "; ".join(
                    f"{i.get('name','?').split('.')[-1]}: {i.get('message','?')}"
                    for i in inputs[:3]
                )
                return {"_error": f"400 {summary}"}
            return {"_error": f"{r.status_code} {r.text[:180]}"}
        except Exception as e:
            logger.error(f"네이버 등록 예외: {e}")
            return {"_error": f"exception: {str(e)[:180]}"}

    logger.error("네이버 등록 태그 재시도 초과")
    return {"_error": "tag retry exceeded"}


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


def get_sale_product_count() -> Optional[int]:
    """네이버 측 실제 SALE 상태 상품 수 (한도 10,000 측정용).

    POST /v1/products/search body={'productStatusTypes':['SALE'], 'page':1, 'size':1}
    응답의 totalElements 가 실제 한도에 카운트되는 상품 수.
    """
    token = _get_token()
    if not token:
        return None
    try:
        r = _request_with_retry(
            "POST",
            BASE + "/v1/products/search",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json;charset=UTF-8",
            },
            json={"productStatusTypes": ["SALE"], "page": 1, "size": 1},
            timeout=15,
        )
        if r is None or r.status_code >= 400:
            return None
        body = r.json() or {}
        return int(body.get("totalElements", 0))
    except Exception as e:
        logger.warning(f"네이버 SALE 카운트 조회 실패: {e}")
        return None


def stop_sales(origin_product_no: str) -> tuple[bool, str]:
    """상품 판매 중지 — update_product partial 로 statusType='SUSPENSION'.

    네이버 originProduct 의 sale 관련 필드는 `statusType` (SALE/SUSPENSION/...).
    한도 회전 (rotation) 용 — 판매 중지된 상품은 10,000 한도에서 빠짐.
    delete 가 아니므로 wing 에서 다시 살릴 수 있음 (재승인 필요).
    """
    if not origin_product_no:
        return False, "originProductNo 없음"
    try:
        result = update_product(
            origin_product_no,
            {"originProduct": {"statusType": "SUSPENSION"}},
        )
        if result is None:
            return False, "update_product 실패 (응답 없음)"
        return True, ""
    except Exception as e:
        return False, f"예외: {e}"


def delete_product(product_no: str) -> tuple[bool, str]:
    """상품 완전 삭제 (DELETE /v2/products/origin-products/{productNo}).

    - 2xx → (True, "")
    - 그 외 → (False, 에러 요약)
    """
    if not product_no:
        return False, "product_no 없음"
    token = _get_token()
    if not token:
        return False, "토큰 발급 실패"
    try:
        r = _request_with_retry(
            "DELETE",
            BASE + f"/v2/products/origin-products/{product_no}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        if r is None:
            return False, "no response"
        if r.status_code < 400:
            return True, ""
        return False, f"status={r.status_code} {r.text[:200]}"
    except Exception as e:
        return False, f"예외: {e}"


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


def get_standard_purchase_option_guides(leaf_category_id: str) -> Optional[dict]:
    """카테고리별 판매 옵션 가이드 조회 — 그룹상품 등록 가능 여부 판단용.

    GET /v2/standard-purchase-option-guides?leafCategoryId={id}

    응답(추정): {"useOptionYn": bool, "guides": [...], ...}
        useOptionYn=true 인 카테고리만 그룹상품 등록 가능.

    v2.45.0 (2024-12-11) 릴리즈.
    """
    token = _get_token()
    if not token:
        return None
    r = _request_with_retry(
        "GET",
        BASE + f"/v2/standard-purchase-option-guides?leafCategoryId={leaf_category_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if r is None or r.status_code >= 400:
        logger.warning(
            f"판매 옵션 가이드 조회 실패 cat={leaf_category_id}: "
            f"{r.status_code if r else 'no-response'} "
            f"{r.text[:200] if r else ''}"
        )
        return None
    return r.json()


def update_product(product_no: str, partial: dict, max_tag_retries: int = 2) -> Optional[dict]:
    """상품 수정 — GET 으로 전체 데이터 가져온 뒤 partial 병합 후 PUT.

    400 + Restricted.sellerTags (금지 태그) 응답을 받으면 해당 태그를 제거하고
    최대 max_tag_retries 회 재시도. register_product 와 동일 패턴.
    """
    token = _get_token()
    if not token:
        return None
    current = get_product(product_no)
    if not current:
        logger.error(f"네이버 상품 조회 실패 (수정 전): {product_no}")
        return None
    for key, val in partial.get("originProduct", {}).items():
        current["originProduct"][key] = val

    for attempt in range(max_tag_retries + 1):
        try:
            r = _request_with_retry(
                "PUT",
                BASE + f"/v2/products/origin-products/{product_no}",
                json=current,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
        except Exception as e:
            logger.error(f"네이버 수정 예외: {e}")
            return None
        if r is None:
            logger.error(f"네이버 상품 수정 실패: no-response")
            return None
        if r.status_code < 400:
            return r.json()

        body = {}
        try:
            body = r.json() or {}
        except (json.JSONDecodeError, ValueError):
            pass

        # 금지 태그 자동 strip + 재시도 (register_product 와 동일 패턴)
        restricted = _extract_restricted_words(body) if isinstance(body, dict) else set()
        if restricted and attempt < max_tag_retries:
            seo = current.get("originProduct", {}).get("detailAttribute", {}).get("seoInfo", {})
            tags = seo.get("sellerTags", [])
            if tags:
                filtered = [t for t in tags if not any(rw in (t.get("text", "") if isinstance(t, dict) else str(t)) for rw in restricted)]
                if len(filtered) < len(tags):
                    seo["sellerTags"] = filtered
                    logger.info(f"[update] {product_no} 금지 태그 strip — 제거 {len(tags)-len(filtered)}개, 재시도 {attempt+1}")
                    continue
            # 태그 자체 비우고 재시도
            seo["sellerTags"] = []
            logger.info(f"[update] {product_no} sellerTags 전체 비움 + 재시도 {attempt+1}")
            continue

        logger.error(f"네이버 상품 수정 실패: {r.status_code} {r.text[:200]}")
        return None
    return None


# ── 주문 조회 (스마트스토어 주문 폴링용) ───────────────────────
def get_changed_product_orders(
    last_changed_from: str,
    last_changed_to: Optional[str] = None,
    last_changed_type: str = "PAYED",
) -> list[str]:
    """변경된 productOrderId 목록 조회.

    last_changed_from / last_changed_to: ISO8601 with offset (예: "2026-04-25T00:00:00.000+09:00")
    last_changed_type:
        PAY_WAITING / PAYED / DISPATCHED / PURCHASE_DECIDED / EXCHANGE_OPTION /
        GIFT_RECEIVED / CLAIM_REJECTED / CANCELED / RETURNED / EXCHANGED /
        COLLECT_DONE / CLAIM_REQUESTED / ADMIN_CANCELING / CANCELED_BY_NOPAYMENT /
        HOPE_DELIVERY_INFO_CHANGED
    """
    token = _get_token()
    if not token:
        return []
    params = {
        "lastChangedFrom": last_changed_from,
        "lastChangedType": last_changed_type,
    }
    if last_changed_to:
        params["lastChangedTo"] = last_changed_to
    try:
        r = _request_with_retry(
            "GET",
            BASE + "/v1/pay-order/seller/product-orders/last-changed-statuses",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except Exception as e:
        logger.error(f"네이버 주문 변경ID 조회 예외: {e}")
        return []
    if r is None or r.status_code >= 400:
        logger.warning(
            f"네이버 주문 변경ID 조회 실패: "
            f"{r.status_code if r else 'no-response'} {r.text[:200] if r else ''}"
        )
        return []
    body = r.json() or {}
    data = body.get("data") or {}
    statuses = data.get("lastChangeStatuses") or []
    return [s.get("productOrderId") for s in statuses if s.get("productOrderId")]


def get_product_order_details(product_order_ids: list[str]) -> list[dict]:
    """productOrderId 리스트 → 주문 상세 리스트.

    네이버 API 한도: 한 번에 최대 300건. 초과 시 배치 분할.
    반환: [{"productOrder": {...}, "order": {...}}, ...]
    """
    if not product_order_ids:
        return []
    token = _get_token()
    if not token:
        return []
    out: list[dict] = []
    BATCH = 300
    for i in range(0, len(product_order_ids), BATCH):
        chunk = product_order_ids[i:i + BATCH]
        try:
            r = _request_with_retry(
                "POST",
                BASE + "/v1/pay-order/seller/product-orders/query",
                json={"productOrderIds": chunk},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=20,
            )
        except Exception as e:
            logger.error(f"네이버 주문 상세 조회 예외: {e}")
            continue
        if r is None or r.status_code >= 400:
            logger.warning(
                f"네이버 주문 상세 조회 실패: "
                f"{r.status_code if r else 'no-response'} {r.text[:300] if r else ''}"
            )
            continue
        body = r.json() or {}
        data = body.get("data") or []
        if isinstance(data, list):
            out.extend(data)
    return out
