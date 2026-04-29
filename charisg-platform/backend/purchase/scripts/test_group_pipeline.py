"""
test_group_pipeline.py — 단일 ASIN으로 변형 그룹 발견 PoC.

사용법:
  cd /home/silverrain/projects/CharisG-Platform/charisg-platform
  .venv/bin/python -m backend.purchase.scripts.test_group_pipeline [ASIN]

기본 ASIN: B0DZ35W76L (FORTIBONE Collagen Peptides)

Phase 1-2 (이번 단계): 입력 ASIN의 parent 추적 → discover_group → 각 child summary.
Phase 3-6 (다음 단계): products/variation_groups 채움 + 카테고리 매핑 + 쿠팡 등록.

DB 영향:
  - variation_groups 테이블에 row 1개 upsert (discover_group 부수효과)
  - products 테이블은 건드리지 않음
"""
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from backend.purchase.services.sp_api_facts import fetch_full_catalog_facts
from backend.purchase.services.sp_api_group_discovery import discover_group


DEFAULT_ASIN = "B0DZ35W76L"


def fetch_child_summary(asin: str) -> dict | None:
    """child ASIN 의 차원값 추출 (size/color/flavor/pack 정보)."""
    facts = fetch_full_catalog_facts(asin, persist=False)
    if not facts:
        return None
    return {
        "asin": asin,
        "title": (facts.get("title_en") or "")[:60],
        "brand": facts.get("brand"),
        "size_label": facts.get("size_label") or facts.get("size_attr"),
        "color": facts.get("color"),
        "flavor": facts.get("flavor_attr"),
        "package_quantity": facts.get("package_quantity"),
        "number_of_items": facts.get("number_of_items"),
        "net_content_value": facts.get("net_content_value"),
        "net_content_unit": facts.get("net_content_unit"),
    }


def main():
    asin = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ASIN).strip().upper()

    print()
    print(f"=== 1단계: {asin} 의 parent_asin 발견 ===")
    facts = fetch_full_catalog_facts(asin, persist=False)
    if not facts:
        print("[FAIL] SP-API 호출 실패")
        return 1

    parent_asin = facts.get("parent_asin")
    print(f"  title       : {(facts.get('title_en') or '')[:80]}")
    print(f"  brand       : {facts.get('brand')}")
    print(f"  size_label  : {facts.get('size_label')}")
    print(f"  package_qty : {facts.get('package_quantity')}")

    if not parent_asin:
        print()
        print("[INFO] variation 없는 단일 상품 (parent_asin = None)")
        print("       → multi-option 통합 등록 대상 아님. 단일 등록 흐름으로 진행.")
        return 0

    print(f"  parent_asin           : {parent_asin}")
    print(f"  variation_theme       : {facts.get('variation_theme')}")
    print(f"  variation_dimensions  : {facts.get('variation_dimensions')}")

    print()
    print(f"=== 2단계: parent {parent_asin} → 형제 child 전체 발견 ===")
    meta = discover_group(parent_asin)
    if not meta:
        print("[FAIL] discover_group 실패")
        return 1

    children = meta.get("child_asins") or []
    print(f"  child_count           : {len(children)}")
    print(f"  variation_theme       : {meta.get('variation_theme')}")
    print(f"  variation_dimensions  : {meta.get('variation_dimensions')}")
    print(f"  base item_name        : {(meta.get('item_name') or '')[:80]}")
    print(f"  childAsins            : {children}")

    print()
    print(f"=== 3단계: 각 child 의 차원값 ({len(children)} ASIN × 0.5s) ===")
    print()
    print(f"{'ASIN':<12} {'size_label':<28} {'pkg_qty':<8} {'#items':<8} {'net':<12} {'title':<60}")
    print("-" * 132)
    for ch in children:
        s = fetch_child_summary(ch)
        if not s:
            print(f"{ch:<12} [fetch 실패]")
            continue
        net = (
            f"{s['net_content_value']} {s['net_content_unit']}"
            if s.get("net_content_value") else "-"
        )
        print(
            f"{s['asin']:<12} "
            f"{str(s.get('size_label') or '-'):<28} "
            f"{str(s.get('package_quantity') or '-'):<8} "
            f"{str(s.get('number_of_items') or '-'):<8} "
            f"{net:<12} "
            f"{s['title']:<60}"
        )

    print()
    print("=== 결정 포인트 ===")
    print("위 결과 보고 다음 중 선택:")
    print("  A) child 들이 Pack of 1/2/3 같이 '수량' 차원이면 → 단일 등록 (Pack of 1만)")
    print("  B) child 들이 Color/Size/Flavor 등 진짜 옵션 차원이면 → multi-option 통합 등록")
    print("  C) 단일 child 만 의미 있고 나머지는 노이즈면 → 단일 등록")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
