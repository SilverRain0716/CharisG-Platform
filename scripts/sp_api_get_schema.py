"""
sp_api_get_schema.py — Product Type Definitions API로 productType 스키마 조회.

실행:
    charisg-platform/.venv/bin/python charisg-platform/scripts/sp_api_get_schema.py ART_EASEL

결과는 /tmp/schema_{productType}.json에 저장.
"""
import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "packages", "backend-shared"))

from backend.dropshipping.services.amazon_sp_api_service import (
    get_credentials, get_marketplace,
)
from sp_api.api import ProductTypeDefinitions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("product_type")
    args = parser.parse_args()

    creds = get_credentials()
    client = ProductTypeDefinitions(credentials=creds, marketplace=get_marketplace())

    marketplace_id = os.environ.get("SP_API_MARKETPLACE_ID", "ATVPDKIKX0DER")

    print(f"▶ Product Type Definitions: {args.product_type} ({marketplace_id})")
    res = client.get_definitions_product_type(
        productType=args.product_type,
        marketplaceIds=marketplace_id,
        requirements="LISTING",
        locale="en_US",
    )
    payload = getattr(res, "payload", res)
    if isinstance(payload, dict):
        link = payload.get("schema", {}).get("link", {}).get("resource")
        schema_verb = payload.get("schema", {}).get("link", {}).get("verb", "GET")
    else:
        link = payload.schema.link.resource if hasattr(payload, "schema") else None
        schema_verb = "GET"

    if not link:
        print("❌ 스키마 링크 없음")
        print(json.dumps(payload, indent=2, default=str))
        return 1

    print(f"▶ 스키마 URL: {link[:100]}...")
    r = requests.get(link, timeout=30)
    r.raise_for_status()
    schema = r.json()

    out = f"/tmp/schema_{args.product_type}.json"
    with open(out, "w") as f:
        json.dump(schema, f, indent=2, ensure_ascii=False)
    print(f"💾 저장: {out}")

    required = schema.get("required", []) or schema.get("allOf", [])
    if isinstance(required, list) and required and isinstance(required[0], str):
        print(f"\n✅ required ({len(required)}):")
        for r in required:
            print(f"   - {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
