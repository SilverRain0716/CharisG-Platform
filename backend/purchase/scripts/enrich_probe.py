"""ASIN enrichment 가능성 증명 — 시트 ASIN 몇 개로 SP-API 카탈로그 + ProductsV0 landed 조회.
읽기전용 (DB 쓰기 없음). 전체 enrichment 임포터 빌드 전 데이터 확인용."""
import os
import sys
import json
from dotenv import load_dotenv
_ROOT = os.environ.get("CHARISG_ROOT", "/home/ubuntu/CharisG-Platform/charisg-platform")
load_dotenv(os.path.join(_ROOT, ".env"))
from backend.purchase import database
from backend_shared.context import register_db_factory
register_db_factory(database.get_db)


def main(asins):
    from backend.purchase.services.image_downloader import fetch_product_info_sp_api
    # ProductsV0 landed
    landed = {}
    try:
        from sp_api.api.products.products_v0 import ProductsV0
        from sp_api.base import Marketplaces
        from backend.dropshipping.services.amazon_sp_api_service import get_credentials
        from backend.purchase.scripts.refresh_landed_prices import _fetch_prices_batch
        client = ProductsV0(credentials=get_credentials(), marketplace=Marketplaces.US)
        landed = _fetch_prices_batch(asins, client)
    except Exception as e:
        print("ProductsV0 landed 조회 실패:", e)

    for a in asins:
        info = fetch_product_info_sp_api(a)
        lp = landed.get(a, {})
        print(f"\n=== {a} ===")
        if not info:
            print("  ★카탈로그 조회 실패 (빈 응답)")
            continue
        print(f"  title: {str(info.get('title'))[:55]}")
        print(f"  brand: {info.get('brand')}")
        print(f"  images: {len(info.get('images') or [])}개")
        print(f"  identifiers: {str(info.get('identifiers'))[:80]}")
        print(f"  amazon_price_usd(list_price): {info.get('amazon_price_usd')}")
        print(f"  ProductsV0 landed: {lp.get('landed')} listing: {lp.get('listing')} shipping: {lp.get('shipping')}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["B08RM44QGV", "B0CF3LHQSM", "B0B63VX4DP"])
