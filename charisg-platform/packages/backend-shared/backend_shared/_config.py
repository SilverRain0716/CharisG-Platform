"""
backend_shared._config — 환경변수 일원화 (.env 직접 로드).

각 API는 자기 .env를 dotenv로 로드한 뒤 backend_shared 모듈을 import한다.
"""
import os
from pathlib import Path

# AI
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
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

# Naver
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
NAVER_DATALAB_CLIENT_ID = os.environ.get("NAVER_DATALAB_CLIENT_ID", "")
NAVER_DATALAB_CLIENT_SECRET = os.environ.get("NAVER_DATALAB_CLIENT_SECRET", "")
NAVER_SEARCHAD_API_KEY = os.environ.get("NAVER_SEARCHAD_API_KEY", "")
NAVER_SEARCHAD_SECRET_KEY = os.environ.get("NAVER_SEARCHAD_SECRET_KEY", "")
NAVER_SEARCHAD_CUSTOMER_ID = os.environ.get("NAVER_SEARCHAD_CUSTOMER_ID", "")
NAVER_COMMERCE_CLIENT_ID = os.environ.get("NAVER_COMMERCE_CLIENT_ID", "")
NAVER_COMMERCE_CLIENT_SECRET = os.environ.get("NAVER_COMMERCE_CLIENT_SECRET", "")

# Coupang
COUPANG_ACCESS_KEY = os.environ.get("COUPANG_ACCESS_KEY", "")
COUPANG_SECRET_KEY = os.environ.get("COUPANG_SECRET_KEY", "")
COUPANG_VENDOR_ID = os.environ.get("COUPANG_VENDOR_ID", "")

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
