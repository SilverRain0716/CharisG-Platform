"""
sp_api_validate_listing.py — Phase C-Validate: VALIDATION_PREVIEW 모드.

Production 엔드포인트에서 putListingsItem(mode=VALIDATION_PREVIEW) 호출.
실제 리스팅은 생성하지 않고, Amazon이 스키마 검증 결과(issues)를 반환한다.

Sandbox와 달리 REAL 마켓플레이스 스키마로 검증하므로 정확도가 높다.
리스팅은 생성되지 않으므로 계정 health에 영향 없음.

실행:
    charisg-platform/.venv/bin/python charisg-platform/scripts/sp_api_validate_listing.py 638
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("product_id", type=int)
    parser.add_argument("--payload", default=None)
    args = parser.parse_args()

    payload_path = args.payload or f"/tmp/dryrun_payload_{args.product_id}.json"
    if not os.path.exists(payload_path):
        print(f"❌ 페이로드 없음: {payload_path}")
        return 1

    with open(payload_path) as f:
        data = json.load(f)

    meta = data["meta"]
    body = data["request"]["body"]
    sku = meta["sku"]
    seller_id = meta["seller_id"]
    marketplace_id = meta["marketplace_id"]

    print("=" * 70)
    print(f"  Phase C-Validate: VALIDATION_PREVIEW (product_id={args.product_id})")
    print("=" * 70)
    print(f"  SKU           : {sku}")
    print(f"  productType   : {meta['product_type']}")
    print(f"  이미지         : {meta['image_count']}장")
    print()
    print("  🔹 VALIDATION_PREVIEW — 리스팅 생성 안 됨, 스키마 검증만")
    print("-" * 70)

    creds = get_credentials()
    client = ListingsItemsV20210801(credentials=creds, marketplace=get_marketplace())

    print("  putListingsItem(mode=VALIDATION_PREVIEW) 호출 중...")

    try:
        result = client.put_listings_item(
            sellerId=seller_id,
            sku=sku,
            marketplaceIds=marketplace_id,
            body=body,
            mode="VALIDATION_PREVIEW",
            issueLocale="en_US",
        )
        resp = getattr(result, "payload", result)
        print()
        print("  ✅ Amazon 검증 응답 수신")
        print()

        status = resp.get("status", "?")
        issues = resp.get("issues", [])

        errors = [i for i in issues if i.get("severity") == "ERROR"]
        warnings = [i for i in issues if i.get("severity") == "WARNING"]

        print(f"  상태    : {status}")
        print(f"  errors  : {len(errors)}")
        print(f"  warnings: {len(warnings)}")

        if errors:
            print(f"\n  ❌ ERRORS ({len(errors)}):")
            for issue in errors:
                print(f"    [{issue.get('code', '?')}] {issue.get('message', '')}")
                attrs = issue.get("attributeNames", [])
                if attrs:
                    print(f"      → fields: {attrs}")
        if warnings:
            print(f"\n  ⚠️ WARNINGS ({len(warnings)}):")
            for issue in warnings:
                print(f"    [{issue.get('code', '?')}] {issue.get('message', '')}")
                attrs = issue.get("attributeNames", [])
                if attrs:
                    print(f"      → fields: {attrs}")

        # 결과 저장
        out_path = f"/tmp/validation_result_{args.product_id}.json"
        with open(out_path, "w") as f:
            json.dump(resp, f, indent=2, ensure_ascii=False, default=str)
        print(f"\n  💾 검증 결과 저장: {out_path}")

        if not errors:
            print(f"\n  ✅ VALIDATION_PREVIEW 통과 — Phase C-Prod 진행 가능!")
            print(f"     다음: sp_api_upload_listing.py {args.product_id} --confirm")
            return 0
        else:
            print(f"\n  ❌ ERROR 있음 — 수정 후 재시도 필요")
            return 1

    except Exception as e:
        err_str = str(e)
        print(f"\n  ❌ 호출 실패: {type(e).__name__}: {err_str[:300]}")

        # SP-API 에러 응답 파싱
        if hasattr(e, 'args') and e.args:
            try:
                err_list = e.args[0] if isinstance(e.args[0], list) else []
                for err in err_list:
                    if isinstance(err, dict):
                        print(f"    code   : {err.get('code', '?')}")
                        print(f"    message: {err.get('message', '?')}")
                        print(f"    details: {err.get('details', '')}")
            except Exception:
                pass
        return 2


if __name__ == "__main__":
    sys.exit(main())
