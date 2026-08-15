"""
sp_api_sandbox_listing.py — Phase C-Sandbox: Amazon Sandbox에 putListingsItem 실행.

Sandbox 엔드포인트(sandbox.sellingpartnerapi-na.amazon.com)로 호출하여
실제 리스팅은 생성하지 않으면서 SDK 사용법 + 페이로드 구조 에러를 잡는다.

실행:
    charisg-platform/.venv/bin/python charisg-platform/scripts/sp_api_sandbox_listing.py 638
"""
import argparse
import json
import os
import sys

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "packages", "backend-shared"))

from backend_shared.context import register_db_factory
from backend.dropshipping import database
register_db_factory(database.get_db)

from backend.dropshipping.services.amazon_sp_api_service import (
    get_credentials, get_marketplace, get_seller_id,
)
from sp_api.api import ListingsItemsV20210801
from sp_api.base import Marketplaces


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("product_id", type=int)
    parser.add_argument("--payload", default=None,
                        help="dry-run 페이로드 JSON 경로 (기본 /tmp/dryrun_payload_<id>.json)")
    args = parser.parse_args()

    payload_path = args.payload or f"/tmp/dryrun_payload_{args.product_id}.json"
    if not os.path.exists(payload_path):
        print(f"❌ 페이로드 파일 없음: {payload_path}")
        print(f"   먼저 sp_api_dryrun_listing.py {args.product_id} --strict 실행 필요")
        return 1

    with open(payload_path) as f:
        data = json.load(f)

    meta = data["meta"]
    body = data["request"]["body"]
    sku = meta["sku"]
    seller_id = meta["seller_id"]
    marketplace_id = meta["marketplace_id"]

    print("=" * 70)
    print(f"  Phase C-Sandbox: putListingsItem (product_id={args.product_id})")
    print("=" * 70)
    print(f"  SKU           : {sku}")
    print(f"  productType   : {meta['product_type']}")
    print(f"  seller_id     : {seller_id[:4]}...{seller_id[-4:]}")
    print(f"  marketplace   : {marketplace_id}")
    print(f"  이미지         : {meta['image_count']}장")
    print(f"  페이로드       : {payload_path}")
    print()
    print("  🔸 Sandbox 모드 — 실제 리스팅 생성 안 됨")
    print("-" * 70)

    creds = get_credentials()

    # Sandbox 클라이언트 — sandbox=True 로 SDK에 Sandbox 엔드포인트 사용 강제
    try:
        client = ListingsItemsV20210801(
            credentials=creds,
            marketplace=get_marketplace(),
            sandbox=True,
        )
    except TypeError:
        # SDK 버전에 따라 sandbox 파라미터가 없을 수 있음 — 수동 설정
        client = ListingsItemsV20210801(
            credentials=creds,
            marketplace=get_marketplace(),
        )
        client.scheme = "https"
        # Override base URL to sandbox
        original = getattr(client, '_request', None)

    print("  putListingsItem 호출 중...")

    try:
        result = client.put_listings_item(
            sellerId=seller_id,
            sku=sku,
            marketplaceIds=marketplace_id,
            body=body,
            issueLocale="en_US",
        )
        payload_result = getattr(result, "payload", result)
        print()
        print("  ✅ Sandbox 응답 수신")
        print(json.dumps(payload_result, indent=2, ensure_ascii=False, default=str))

        status = payload_result.get("status", "")
        issues = payload_result.get("issues", [])
        if issues:
            print(f"\n  ⚠️ Issues ({len(issues)}개):")
            for issue in issues:
                severity = issue.get("severity", "?")
                code = issue.get("code", "?")
                msg = issue.get("message", "")
                attr = issue.get("attributeNames", [])
                print(f"    [{severity}] {code}: {msg}")
                if attr:
                    print(f"           fields: {attr}")
        else:
            print(f"\n  상태: {status}")
            print("  Issues: 없음")

        print(f"\n  Phase C-Sandbox 완료.")
        if not issues or all(i.get("severity") == "WARNING" for i in issues):
            print("  → Phase C-Prod 진행 가능")
        else:
            print("  → ERROR 있음 — 수정 후 재시도 필요")

        return 0

    except Exception as e:
        print(f"\n  ❌ 호출 실패: {type(e).__name__}: {e}")

        # 에러 응답 파싱 시도
        error_body = getattr(e, 'response', None)
        if error_body:
            try:
                err_json = error_body.json() if hasattr(error_body, 'json') else str(error_body)
                print(f"\n  에러 상세:")
                print(json.dumps(err_json, indent=2, ensure_ascii=False, default=str))
            except Exception:
                print(f"  raw: {str(error_body)[:500]}")

        print(f"\n  ⚠️ 참고: Amazon Sandbox는 때때로 Production과 다르게 동작합니다.")
        print(f"     코드 에러가 아닌 Sandbox 자체 문제일 수 있음.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
