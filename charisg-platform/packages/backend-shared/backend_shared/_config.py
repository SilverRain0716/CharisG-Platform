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

# Discord (모노리스 키명 그대로)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# Coupang
COUPANG_ACCESS_KEY = os.environ.get("COUPANG_ACCESS_KEY", "")
COUPANG_SECRET_KEY = os.environ.get("COUPANG_SECRET_KEY", "")
COUPANG_VENDOR_ID = os.environ.get("COUPANG_VENDOR_ID", "")
# WING 로그인 사용자 ID (vendorUserId 용). vendor_id와 다른 별도 값.
COUPANG_USER_ID = os.environ.get("COUPANG_USER_ID", "")
# 출고지/반품지 코드 — Phase A setup_coupang_logistics.py 1회 실행으로 발급/저장
COUPANG_OUTBOUND_SHIPPING_PLACE_CODE = os.environ.get("COUPANG_OUTBOUND_SHIPPING_PLACE_CODE", "")
COUPANG_RETURN_CENTER_CODE = os.environ.get("COUPANG_RETURN_CENTER_CODE", "")

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
