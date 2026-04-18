"""
generate_listing_content.py — Phase C-0 실행기.

단일 상품에 대해 Amazon 리스팅 콘텐츠를 AI 생성하고 listings 테이블에 저장.

실행:
    charisg-platform/.venv/bin/python charisg-platform/scripts/generate_listing_content.py 842
    charisg-platform/.venv/bin/python charisg-platform/scripts/generate_listing_content.py 842 --save
"""
import argparse
import asyncio
import json
import os
import sys

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "packages", "backend-shared"))

from backend_shared.context import register_db_factory  # noqa: E402
from backend.dropshipping import database  # noqa: E402
register_db_factory(database.get_db)

from backend.dropshipping.services.amazon_listing_content_service import (  # noqa: E402
    generate_listing_content,
    save_listing_content,
)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("product_id", type=int)
    parser.add_argument("--save", action="store_true", help="listings 테이블에 저장")
    args = parser.parse_args()

    print("=" * 70)
    print(f"  Phase C-0: Amazon 리스팅 콘텐츠 생성  (product_id={args.product_id})")
    print("=" * 70)

    content = await generate_listing_content(args.product_id)

    if content.get("error"):
        print(f"❌ {content['error']}")
        if content.get("raw_response"):
            print(f"\n--- raw response (truncated) ---\n{content['raw_response']}")
        return 1

    print(f"\n▶ Title ({len(content.get('title', ''))} chars)")
    print(f"  {content.get('title')}")

    print(f"\n▶ Bullets ({len(content.get('bullets', []))}개)")
    for i, b in enumerate(content.get("bullets", []), 1):
        print(f"  {i}. [{len(b)} chars] {b}")

    desc = content.get("description", "")
    print(f"\n▶ Description ({len(desc)} chars)")
    print("  " + desc.replace("\n", "\n  ")[:800] + ("..." if len(desc) > 800 else ""))

    st = content.get("search_terms", "")
    print(f"\n▶ Search terms ({len(st.encode('utf-8'))} bytes)")
    print(f"  {st}")

    print(f"\n▶ Brand: {content.get('brand')}")

    v = content.get("validation", {})
    print(f"\n▶ Validation: {'✅ OK' if v.get('ok') else '⚠️ 경고 있음'}")
    for w in v.get("warnings", []):
        print(f"  - {w}")

    if args.save:
        listing_id = await save_listing_content(args.product_id, content)
        print(f"\n💾 listings 테이블 저장 완료 (listing_id={listing_id})")
    else:
        print("\n(저장하려면 --save 옵션 추가)")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
