"""
sp_api_smoke_test.py — Phase B: SP-API 연결 확인.

getMarketplaceParticipations 호출 → 인증 3종 세트(LWA client id/secret + refresh token)가
정상 동작하는지 검증. 실패 시 에러 메시지로 원인 추정 가능.

실행:
    cd ~/projects/CharisG-Platform
    charisg-platform/.venv/bin/python charisg-platform/scripts/sp_api_smoke_test.py
"""
import json
import os
import sys

# .env 로드
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

# backend 를 import path 에 추가
sys.path.insert(0, ROOT)

from backend.dropshipping.services.amazon_sp_api_service import (
    get_credentials, get_marketplace, get_seller_id,
)
from sp_api.api import Sellers


def mask(s: str, keep: int = 4) -> str:
    if not s:
        return "(missing)"
    return s[:keep] + "..." + s[-keep:] if len(s) > keep * 2 else "***"


def main() -> int:
    print("=" * 60)
    print("  Amazon SP-API 연결 테스트 (Phase B)")
    print("=" * 60)

    try:
        creds = get_credentials()
        mp = get_marketplace()
        seller_id = get_seller_id()
    except RuntimeError as e:
        print(f"❌ 환경변수 에러: {e}")
        return 1

    print(f"  LWA client id : {mask(creds['lwa_app_id'])}")
    print(f"  refresh token : {mask(creds['refresh_token'])}")
    print(f"  seller id     : {mask(seller_id)}")
    print(f"  marketplace   : {mp.name} ({mp.marketplace_id})")
    print("-" * 60)
    print("  Sellers.get_marketplace_participation() 호출 중...")

    try:
        client = Sellers(credentials=creds, marketplace=mp)
        result = client.get_marketplace_participation()
    except Exception as e:
        print(f"❌ 호출 실패: {type(e).__name__}: {e}")
        print()
        print("원인 추정:")
        print("  - InvalidSignature / Unauthorized → refresh_token 또는 LWA secret 불일치")
        print("  - AccessDenied → 앱 role 승인 누락 or self-auth 미완료")
        print("  - 403 + quota → 앱 publish 전 rate limit 걸림 (정상, 재시도)")
        return 2

    print("✅ 호출 성공")
    print()
    payload = getattr(result, "payload", result)
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    print()
    print("Phase B 완료. 다음 Phase C (리스팅 업로드) 진행 가능.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
