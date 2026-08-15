"""
backend_shared._config — 환경변수 일원화 (.env 직접 로드).

각 API는 자기 .env를 dotenv로 로드한 뒤 backend_shared 모듈을 import한다.
"""
import os
from pathlib import Path

# AI
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# Fallback 키 (무료 등급 등) — 주 키 quota 초과 시 _call_gemini 가 자동 swap
GEMINI_API_KEY_FALLBACK = os.environ.get("GEMINI_API_KEY_FALLBACK", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_PROVIDER = os.environ.get("AI_PROVIDER", "gemini")

# Google Sheets
SHEET_ID = os.environ.get("SHEET_ID", "")
GOOGLE_SA_KEY_PATH = os.environ.get("GOOGLE_SA_KEY_PATH", "")

# GitHub
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")

# Project root (for path resolution by callers)
PROJECT_ROOT = Path(os.environ.get("CHARISG_ROOT", str(Path(__file__).resolve().parents[3])))

# Shopify
SHOPIFY_DOMAIN = os.environ.get("SHOPIFY_DOMAIN", "")
SHOPIFY_TOKEN = os.environ.get("SHOPIFY_TOKEN", "")

# CJ
CJ_EMAIL = os.environ.get("CJ_EMAIL", "")
CJ_PASSWORD = os.environ.get("CJ_PASSWORD", "")
CJ_API_KEY = os.environ.get("CJ_API_KEY", "")

# Naver — 모노리스 호환 fallback 적용
# 모노리스는 NAVER_CLIENT_ID/SECRET 1쌍으로 스마트스토어 (커머스) 인증을 처리했음.
# 우리 새 스키마는 데이터랩 / 검색광고 / 커머스 3개 영역으로 분리한 키 이름을 쓴다.
# COMMERCE_* 가 비어있으면 모노리스의 NAVER_CLIENT_ID/SECRET 로 폴백.
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")

NAVER_DATALAB_CLIENT_ID = (
    os.environ.get("NAVER_DATALAB_CLIENT_ID")
    or NAVER_CLIENT_ID  # 모노리스 폴백 (단일 키 재사용)
    or ""
)
NAVER_DATALAB_CLIENT_SECRET = (
    os.environ.get("NAVER_DATALAB_CLIENT_SECRET")
    or NAVER_CLIENT_SECRET
    or ""
)

NAVER_SEARCHAD_API_KEY = os.environ.get("NAVER_SEARCHAD_API_KEY", "")
NAVER_SEARCHAD_SECRET_KEY = os.environ.get("NAVER_SEARCHAD_SECRET_KEY", "")
NAVER_SEARCHAD_CUSTOMER_ID = os.environ.get("NAVER_SEARCHAD_CUSTOMER_ID", "")

NAVER_COMMERCE_CLIENT_ID = (
    os.environ.get("NAVER_COMMERCE_CLIENT_ID")
    or NAVER_CLIENT_ID  # 모노리스가 NAVER_CLIENT_ID 로 커머스 호출
    or ""
)
NAVER_COMMERCE_CLIENT_SECRET = (
    os.environ.get("NAVER_COMMERCE_CLIENT_SECRET")
    or NAVER_CLIENT_SECRET
    or ""
)

# Naver 스마트스토어 — 계정 선택 스위치 (old=기존 스토어, new=카리스 글로벌 2026-07-30 신설)
#   NAVER_ACTIVE=old|new (기본 old). new면 NAVER_NEW_COMMERCE_* 우선, 누락 시 기존값 폴백.
#   쿠팡 coupang_cfg 와 동일 패턴. 위 상수는 하위호환용으로 그대로 둔다(= old 값).
NAVER_ACTIVE = os.environ.get("NAVER_ACTIVE", "old").strip().lower()


def naver_cfg(name: str, account: str | None = None) -> str:
    """계정 명시 네이버 커머스 자격증명 조회. account=None이면 NAVER_ACTIVE 기본.

    name: 'CLIENT_ID' | 'CLIENT_SECRET'
    new → NAVER_NEW_COMMERCE_<name>, 누락 시 old 로 폴백.
    old → NAVER_COMMERCE_<name>, 누락 시 모노리스 NAVER_<name> 폴백(기존 동작 유지).
    """
    acct = (account or NAVER_ACTIVE or "old").strip().lower()
    if acct == "new":
        # 자격증명은 NAVER_NEW_COMMERCE_*, 주소록 등 설정은 NAVER_NEW_* 로 받는다.
        v = os.environ.get("NAVER_NEW_COMMERCE_" + name) or os.environ.get("NAVER_NEW_" + name)
        if v:
            return v
    return (
        os.environ.get("NAVER_COMMERCE_" + name)
        or os.environ.get("NAVER_" + name)
        or ""
    )

# Discord (모노리스 키명 그대로)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# Coupang — ★2026-08-12 구계정(A00353099) 영구정지. 신계정(A01731680) 전용으로 전환.
#   기본값을 new 로 바꾸고, 구계정 접근은 **예외로 중단**시킨다.
#   종전엔 기본이 old 라 컨텍스트를 안 감싼 호출이 전부 정지 계정으로 갔고,
#   COUPANG_NEW_* 가 없으면 구계정 키로 조용히 폴백까지 했다 —
#   "실패"가 아니라 "잘못된 계정으로 성공"을 만드는 구조였다.
#   과거 데이터 조회는 로컬 DB 로 한다(API 불필요).
COUPANG_ACTIVE = os.environ.get("COUPANG_ACTIVE", "new").strip().lower()

#   임시로 열어야 하면 COUPANG_ALLOW_OLD=1 (기록 남기고 쓸 것)
COUPANG_ALLOW_OLD = os.environ.get("COUPANG_ALLOW_OLD", "").strip() in ("1", "true", "yes")


class CoupangOldAccountClosed(RuntimeError):
    """구계정은 영구정지라 접근을 막았다. 로컬 DB 를 쓰거나 신계정으로 호출할 것."""


def coupang_cfg(name: str, account: str | None = None) -> str:
    """계정 명시 자격증명/설정 조회. account=None 이면 COUPANG_ACTIVE(기본 new).

    ★구계정은 막혀 있다. ★COUPANG_NEW_* 누락 시 구계정 폴백도 없앴다 —
      폴백이 있으면 키 하나 빠졌을 때 정지 계정으로 호출이 나간다.
    """
    acct = (account or COUPANG_ACTIVE or "new").strip().lower()
    if acct == "old" and not COUPANG_ALLOW_OLD:
        raise CoupangOldAccountClosed(
            "쿠팡 구계정(A00353099)은 2026-08-12 영구정지로 폐쇄됨. "
            "신계정을 쓰거나 로컬 DB 를 조회할 것 (강제로 열려면 COUPANG_ALLOW_OLD=1)")
    if acct == "new":
        v = os.environ.get("COUPANG_NEW_" + name)
        if v:
            return v
        # ★구계정 폴백 금지. 없으면 없는 대로 빈 값을 주고, 호출부가 터지게 둔다.
        return ""
    return os.environ.get("COUPANG_" + name, "")


def _cpg(name: str) -> str:
    return coupang_cfg(name, COUPANG_ACTIVE)


COUPANG_ACCESS_KEY = _cpg("ACCESS_KEY")
COUPANG_SECRET_KEY = _cpg("SECRET_KEY")
COUPANG_VENDOR_ID = _cpg("VENDOR_ID")
# WING 로그인 사용자 ID (vendorUserId 용). vendor_id와 다른 별도 값.
COUPANG_USER_ID = _cpg("USER_ID")
# 출고지/반품지 코드 — Phase A setup_coupang_logistics.py 1회 실행으로 발급/저장
COUPANG_OUTBOUND_SHIPPING_PLACE_CODE = _cpg("OUTBOUND_SHIPPING_PLACE_CODE")
COUPANG_RETURN_CENTER_CODE = _cpg("RETURN_CENTER_CODE")

# 11번가 (2026-08-08 개통, 2026-08-11 2계정화)
#   ★폴백을 두지 않는다. 쿠팡·네이버의 cfg 는 값이 비면 `or` 로 다른 계정 값을 잡는데,
#   이름에 오타가 나면 조용히 다른 계정으로 새는 함정이 있다. 11번가는 빈 값을 그대로
#   내보내고 elevenst_service._key() 가 호출 시점에 죽인다.
#   유효 키는 32자. 로컬 .env 사본에 남은 40자 키는 폐기값이라 -200 을 뱉는다.
#
#   계정 구분: new = 카리스 글로벌(스카이포트, memNo 76614773)
#             old = 카리스G(memNo 68232815)
ELEVENST_ACTIVE = os.environ.get("ELEVENST_ACTIVE", "new").strip().lower()


def elevenst_cfg(name: str, account: str | None = None) -> str:
    """계정 명시 자격증명 조회. account=None 이면 ELEVENST_ACTIVE 기본.

    new → ELEVENST_<name>, old → ELEVENST_OLD_<name>.
    ★교차 폴백 없음 — 키가 없으면 빈 문자열이다. 없는 계정으로 호출하면
      다른 계정 상품에 손대는 것보다 즉시 실패하는 편이 낫다.
    """
    acct = (account or ELEVENST_ACTIVE or "new").strip().lower()
    prefix = "ELEVENST_OLD_" if acct == "old" else "ELEVENST_"
    return os.environ.get(prefix + name, "")


ELEVENST_API_KEY = elevenst_cfg("API_KEY")
# 계정 앵커. 11번가엔 로그인ID 를 돌려주는 API 가 없어 주소록의 memNo 로만 계정을 식별한다.
ELEVENST_MEM_NO = elevenst_cfg("MEM_NO") or ("68232815" if ELEVENST_ACTIVE == "old" else "76614773")

# Webshare proxy (DS crawler)
PROXY_HOST = os.environ.get("PROXY_HOST", "")
PROXY_PORT = os.environ.get("PROXY_PORT", "")
PROXY_USER_BASE = os.environ.get("PROXY_USER_BASE", "")
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD", "")

# JWT (Hub)
JWT_SECRET = os.environ.get("JWT_SECRET", "change-me-in-production")
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_HOURS = int(os.environ.get("JWT_EXPIRE_HOURS", "168"))  # 7일

# Auth bypass (개발용)
AUTH_BYPASS = os.environ.get("CTRL_AUTH_BYPASS", "false").lower() == "true"

# 환율 API
EXCHANGE_RATE_API = os.environ.get("EXCHANGE_RATE_API", "")

# 공개 베이스 URL (이미지 등 외부에서 pull 가능한 HTTPS origin)
# 쿠팡이 image_cache.public_url(/api/pa/images/...)을 pull할 때 절대 URL로 변환.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://wongbigo.com")

# 솔라피 (문자 발송)
# 운영 키는 IP 화이트리스트(52.78.174.31/32)가 걸려 있어 EC2 에서만 동작한다.
# 로컬 개발/조회용으로 IP 제한 없는 DEV 키를 따로 두고, 운영 키가 없을 때만 폴백.
SOLAPI_API_KEY = os.environ.get("SOLAPI_API_KEY", "") or os.environ.get("SOLAPI_DEV_API_KEY", "")
SOLAPI_API_SECRET = os.environ.get("SOLAPI_API_SECRET", "") or os.environ.get("SOLAPI_DEV_API_SECRET", "")
SOLAPI_SENDER = os.environ.get("SOLAPI_SENDER", "")
# 본문 앞에 붙일 상호 접두어. 기본 비활성 — 90byte 예산을 먹기 때문.
SOLAPI_SMS_PREFIX = os.environ.get("SOLAPI_SMS_PREFIX", "")
