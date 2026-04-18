"""
amazon_sp_api_service.py — DS 전용 Amazon Selling Partner API 클라이언트.

Phase A: 자격증명 로더 + 클라이언트 팩토리.
Phase B: getMarketplaceParticipations 연결 테스트 (scripts/sp_api_smoke_test.py에서 호출).
Phase C+: Listings Items / Orders / Pricing 래퍼.

환경변수 (.env):
    SP_API_LWA_CLIENT_ID
    SP_API_LWA_CLIENT_SECRET
    SP_API_REFRESH_TOKEN
    SP_API_SELLER_ID
    SP_API_MARKETPLACE_ID   (예: ATVPDKIKX0DER = US)
"""
import os
from functools import lru_cache

from sp_api.base import Marketplaces


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"환경변수 {key} 가 설정돼 있지 않습니다 (.env 확인)")
    return val


@lru_cache(maxsize=1)
def get_credentials() -> dict:
    """python-amazon-sp-api 가 요구하는 credentials dict 반환."""
    return {
        "lwa_app_id":        _require("SP_API_LWA_CLIENT_ID"),
        "lwa_client_secret": _require("SP_API_LWA_CLIENT_SECRET"),
        "refresh_token":     _require("SP_API_REFRESH_TOKEN"),
    }


def get_marketplace() -> Marketplaces:
    """SP_API_MARKETPLACE_ID → Marketplaces enum 변환. 기본 US."""
    mp_id = os.environ.get("SP_API_MARKETPLACE_ID", "ATVPDKIKX0DER")
    for mp in Marketplaces:
        if mp.marketplace_id == mp_id:
            return mp
    return Marketplaces.US


def get_seller_id() -> str:
    return _require("SP_API_SELLER_ID")
