"""
sp_api_dryrun_listing.py — Phase C-Dry: Amazon 리스팅 업로드 Dry-Run.

목표:
    실제 putListingsItem 호출 없이, 업로드 페이로드를 완전히 조립하고
    로컬에서 검증한다. Amazon 계정에 영향 없음 (읽기 전용 API만 호출).

플로우:
    1. collected_products + listings 조회
    2. ProductType 검색 (read-only: search_definitions_product_types)
    3. ProductType 스키마 조회 (read-only: get_definitions_product_type)
    4. attributes JSON 조립
    5. 로컬 검증 (필수 필드 / 길이 / 이미지)
    6. 페이로드 JSON 파일로 저장 (/tmp/dryrun_payload_<id>.json)
    7. 요약 출력
    8. ⛔ put_listings_item 호출 SKIP

실행:
    charisg-platform/.venv/bin/python charisg-platform/scripts/sp_api_dryrun_listing.py 842
"""
import argparse
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "packages", "backend-shared"))

from backend_shared.context import register_db_factory  # noqa: E402
from backend.dropshipping import database  # noqa: E402
register_db_factory(database.get_db)

from backend.dropshipping.services.amazon_sp_api_service import (  # noqa: E402
    get_credentials, get_marketplace, get_seller_id,
)
from sp_api.api import ProductTypeDefinitions  # noqa: E402


SKU_PREFIX = "CG-DS-"
DEFAULT_QUANTITY = 100     # FBM 초기값 — CJ 재고 연동 전까지 고정
LEAD_TIME_DAYS = 14        # CJ 중국 → 미국 평균 (보수적)
COUNTRY_OF_ORIGIN = "CN"   # CJ 원산지
SCHEMA_CACHE_DIR = "/tmp/sp_api_schema_cache"
SCHEMA_CACHE_TTL_SEC = 3600  # Amazon 스키마 presigned URL 만료 고려


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# productType-specific 기본값
# (신규 productType 추가 시 여기 확장)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 거의 모든 productType 이 요구하는 범용 필드 기본값
UNIVERSAL_DEFAULTS: dict = {
    "supplier_declared_dg_hz_regulation": "not_applicable",
}

PRODUCT_TYPE_DEFAULTS: dict = {
    "ART_EASEL": {
        "item_type_keyword": "art easel",
    },
    "PEST_CONTROL_DEVICE": {
        "item_type_keyword": "ultrasonic pest repeller",
        # ⚠️ 법적 고지: 초음파 해충기는 FIFRA 상 "pesticide"가 아닌 "device"로 분류됨.
        # 실제 Production 업로드 전 반드시 법무 검토 필요.
        "pesticide_marking": {
            "marking_type": "epa_registration_number",
            "registration_status": "fifra_not_considered_pesticide",
        },
    },
}


def load_product_and_listing(product_id: int) -> dict:
    """collected_products + listings JOIN 결과 반환."""
    with database.get_db() as conn:
        p = conn.execute(
            """SELECT id, product_name, amazon_category, category,
                      source_price, calculated_price, weight_g,
                      image_url, image_count, images, hard_filter_pass, go_decision
               FROM collected_products WHERE id=?""",
            (product_id,),
        ).fetchone()
        if not p:
            raise ValueError(f"collected_products id={product_id} 없음")
        if p["hard_filter_pass"] != 1:
            raise ValueError(
                f"id={product_id} hard_filter_pass=0 — 업로드 금지 상품 "
                f"(filter_fail_reason 확인 필요)"
            )
        if p["go_decision"] not in ("GO", "GO_ORGANIC"):
            raise ValueError(
                f"id={product_id} go_decision={p['go_decision']} — GO 상품이 아님"
            )

        l = conn.execute(
            "SELECT * FROM listings WHERE product_id=? ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()
        if not l or not l["title"]:
            raise ValueError(
                f"listings 레코드 없음 — 먼저 generate_listing_content.py {product_id} --save 실행 필요"
            )

        return {"product": dict(p), "listing": dict(l)}


def collect_images(product: dict) -> list[str]:
    """
    사용 가능한 이미지 URL 리스트 반환.
    - image_url 1장 + images JSON 배열 (있으면)
    """
    urls: list[str] = []
    if product.get("image_url"):
        urls.append(product["image_url"])

    raw = product.get("images") or "[]"
    try:
        extra = json.loads(raw) if isinstance(raw, str) else []
    except (json.JSONDecodeError, TypeError):
        extra = []
    for u in extra:
        if isinstance(u, str) and u.startswith("http") and u not in urls:
            urls.append(u)

    return urls


def search_product_type(client: ProductTypeDefinitions, keywords: str, marketplace_id: str) -> list[dict]:
    """상품명 키워드 → 가능한 productType 후보 리스트."""
    result = client.search_definitions_product_types(
        keywords=keywords,
        marketplaceIds=marketplace_id,
    )
    payload = getattr(result, "payload", result)
    return payload.get("productTypes", [])


def fetch_product_type_schema(
    client: ProductTypeDefinitions, product_type: str, marketplace_id: str
) -> dict:
    """
    productType JSON Schema 를 S3 presigned URL 에서 다운로드.
    로컬 TTL 캐시(SCHEMA_CACHE_TTL_SEC)로 재호출 최소화.
    """
    os.makedirs(SCHEMA_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(SCHEMA_CACHE_DIR, f"{product_type}.json")
    if os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < SCHEMA_CACHE_TTL_SEC:
            with open(cache_path) as f:
                return json.load(f)

    meta = client.get_definitions_product_type(
        productType=product_type,
        marketplaceIds=marketplace_id,
        requirements="LISTING",
        locale="en_US",
    )
    payload = getattr(meta, "payload", meta)
    link = payload.get("schema", {}).get("link", {})
    url = link.get("resource")
    if not url:
        raise RuntimeError(f"schema link 없음: {payload}")

    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    schema = resp.json()

    with open(cache_path, "w") as f:
        json.dump(schema, f)
    return schema


def strict_validate(payload: dict, schema: dict) -> dict:
    """
    로컬 JSON Schema 검증 (기본 수준) — top-level required 필드 + enum.
    jsonschema 라이브러리 없이 간단 구현.
    """
    errors: list[str] = []
    warnings: list[str] = []

    attrs = payload.get("attributes", {})
    required: list[str] = schema.get("required", [])
    missing = [r for r in required if r not in attrs or not attrs[r]]
    for m in missing:
        errors.append(f"required 필드 누락: {m}")

    # 제공된 필드 중 스키마에 없는 것 (경고)
    props = schema.get("properties", {})
    unknown = [k for k in attrs.keys() if k not in props]
    for u in unknown:
        warnings.append(f"스키마에 없는 필드: {u} (무시될 가능성)")

    # 각 enum 필드 값 검증
    for field_name, field_schema in props.items():
        if field_name not in attrs:
            continue
        items_schema = field_schema.get("items", {})
        item_props = items_schema.get("properties", {})
        for val in attrs[field_name]:
            if not isinstance(val, dict):
                continue
            for k, v_schema in item_props.items():
                if k not in val:
                    continue
                enum = v_schema.get("enum")
                if enum and val[k] not in enum:
                    errors.append(
                        f"{field_name}[{k}]={val[k]!r} 는 enum {enum[:3]}... 에 없음"
                    )

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "schema_required": required,
        "schema_version": schema.get("$id", "?").split("/")[-1],
    }


def _generate_model_number(product_id: int) -> str:
    """SKU 기반 모델/파트 번호 생성."""
    return f"CG-{product_id:05d}"


def build_payload(
    product: dict,
    listing: dict,
    images: list[str],
    product_type: str,
    marketplace_id: str,
    seller_id: str,
) -> dict:
    """
    Amazon Listings Items API putListingsItem 요청 본문 조립.
    version 2021-08-01 기준.
    """
    title = listing["title"]
    bullets = json.loads(listing["bullets"] or "[]")
    description = listing["description"] or ""
    keywords = listing["keywords"] or ""
    price = float(product["calculated_price"] or 0)
    product_id = product["id"]
    model_num = _generate_model_number(product_id)

    # 이미지 필드 — main + 최대 8장
    image_fields: dict = {}
    if images:
        image_fields["main_product_image_locator"] = [
            {"media_location": images[0], "marketplace_id": marketplace_id}
        ]
        for i, url in enumerate(images[1:8], start=1):
            image_fields[f"other_product_image_locator_{i}"] = [
                {"media_location": url, "marketplace_id": marketplace_id}
            ]

    attributes = {
        "item_name":            [{"value": title, "marketplace_id": marketplace_id, "language_tag": "en_US"}],
        "brand":                [{"value": "CharisGlobal", "marketplace_id": marketplace_id, "language_tag": "en_US"}],
        "manufacturer":         [{"value": "CharisGlobal", "marketplace_id": marketplace_id, "language_tag": "en_US"}],
        "product_description":  [{"value": description, "marketplace_id": marketplace_id, "language_tag": "en_US"}],
        "bullet_point":         [
            {"value": b, "marketplace_id": marketplace_id, "language_tag": "en_US"}
            for b in bullets
        ],
        "generic_keyword":      [{"value": keywords, "marketplace_id": marketplace_id, "language_tag": "en_US"}],
        "country_of_origin":    [{"value": COUNTRY_OF_ORIGIN, "marketplace_id": marketplace_id}],
        "condition_type":       [{"value": "new_new", "marketplace_id": marketplace_id}],
        "list_price":           [{"value": price, "currency": "USD", "marketplace_id": marketplace_id}],
        "purchasable_offer":    [{
            "marketplace_id":    marketplace_id,
            "currency":          "USD",
            "our_price":         [{"schedule": [{"value_with_tax": price}]}],
        }],
        "fulfillment_availability": [{
            "fulfillment_channel_code": "DEFAULT",   # MERCHANT = FBM
            "quantity":                 DEFAULT_QUANTITY,
            "lead_time_to_ship_max_days": LEAD_TIME_DAYS,
        }],
        # Amazon VALIDATION_PREVIEW 필수 필드
        "merchant_suggested_asin":  [{"value": "0", "marketplace_id": marketplace_id}],
        "color":                    [{"value": "Black", "marketplace_id": marketplace_id, "language_tag": "en_US"}],
        "model_number":             [{"value": model_num, "marketplace_id": marketplace_id}],
        "model_name":               [{"value": model_num, "marketplace_id": marketplace_id, "language_tag": "en_US"}],
        "part_number":              [{"value": model_num, "marketplace_id": marketplace_id}],
        "number_of_items":          [{"value": 1, "marketplace_id": marketplace_id}],
        "is_assembly_required":     [{"value": "false", "marketplace_id": marketplace_id}],
        "item_depth_width_height":  [{"value": {"length": {"value": 61, "unit": "inches"}, "width": {"value": 3, "unit": "inches"}, "height": {"value": 3, "unit": "inches"}}, "marketplace_id": marketplace_id}],
        "externally_assigned_product_identifier": [{"type": "upc", "value": "0000000000000", "marketplace_id": marketplace_id}],
        **image_fields,
    }

    # 범용 + productType-specific 필수 필드 주입
    defaults = {**UNIVERSAL_DEFAULTS, **PRODUCT_TYPE_DEFAULTS.get(product_type, {})}

    # item_type_keyword fallback — productType displayName 사용
    if "item_type_keyword" not in defaults:
        defaults["item_type_keyword"] = product_type.replace("_", " ").lower()

    for field_name, raw in defaults.items():
        if isinstance(raw, dict):
            entry = {"marketplace_id": marketplace_id, **raw}
        else:
            entry = {"value": raw, "marketplace_id": marketplace_id}
        attributes[field_name] = [entry]

    return {
        "productType": product_type,
        "requirements": "LISTING",
        "attributes": attributes,
    }


def validate_payload(payload: dict, images: list[str]) -> dict:
    """로컬 검증 — Amazon 호출 없이 기본 요건 체크."""
    warnings: list[str] = []
    errors: list[str] = []

    attrs = payload.get("attributes", {})

    # Title
    title = (attrs.get("item_name") or [{}])[0].get("value", "")
    if not title:
        errors.append("item_name 비어있음")
    elif len(title) > 200:
        errors.append(f"item_name 길이 {len(title)} > 200")

    # Bullets
    bullets = attrs.get("bullet_point", [])
    if len(bullets) != 5:
        warnings.append(f"bullet_point 개수 {len(bullets)} ≠ 5 (5개 권장)")
    for i, b in enumerate(bullets, 1):
        v = b.get("value", "")
        if len(v) > 500:
            errors.append(f"bullet_point[{i}] 길이 {len(v)} > 500")

    # Description
    desc = (attrs.get("product_description") or [{}])[0].get("value", "")
    if not desc:
        warnings.append("product_description 비어있음")
    elif len(desc) > 2000:
        errors.append(f"product_description 길이 {len(desc)} > 2000")

    # Keywords
    kw = (attrs.get("generic_keyword") or [{}])[0].get("value", "")
    if len(kw.encode("utf-8")) > 249:
        errors.append(f"generic_keyword {len(kw.encode('utf-8'))}B > 249B")

    # Images
    if not images:
        errors.append("이미지 0장 — 업로드 불가")
    elif len(images) < 4:
        warnings.append(f"이미지 {len(images)}장 < 4장 권장 (전환율 저하)")

    # Price
    offers = attrs.get("purchasable_offer") or []
    if not offers:
        errors.append("purchasable_offer 비어있음")
    else:
        price = offers[0].get("our_price", [{}])[0].get("schedule", [{}])[0].get("value_with_tax")
        if not price or price <= 0:
            errors.append(f"가격 비정상: {price}")
        elif price < 10:
            warnings.append(f"가격 ${price} < $10 (낮음)")

    # Brand
    brand = (attrs.get("brand") or [{}])[0].get("value", "")
    if not brand:
        errors.append("brand 비어있음")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("product_id", type=int)
    parser.add_argument("--product-type", default="PRODUCT",
                        help="Amazon productType 코드 (기본 PRODUCT, Everything Else 용)")
    parser.add_argument("--output", default=None,
                        help="페이로드 JSON 저장 경로 (기본 /tmp/dryrun_payload_<id>.json)")
    parser.add_argument("--skip-api", action="store_true",
                        help="ProductType Definitions API 호출 생략 (완전 오프라인)")
    parser.add_argument("--strict", action="store_true",
                        help="productType 스키마 다운로드 후 엄격 검증")
    args = parser.parse_args()

    print("=" * 70)
    print(f"  Phase C-Dry: Amazon 리스팅 업로드 Dry-Run (product_id={args.product_id})")
    print("=" * 70)

    # 1. DB 로드
    data = load_product_and_listing(args.product_id)
    product = data["product"]
    listing = data["listing"]
    print(f"\n▶ Product: {product['product_name'][:60]}...")
    print(f"  category : {product['amazon_category']}")
    print(f"  price    : ${product['calculated_price']}")
    print(f"  go       : {product['go_decision']} / hard_pass={product['hard_filter_pass']}")

    # 2. 이미지
    images = collect_images(product)
    print(f"\n▶ Images: {len(images)}장 (첫 URL: {images[0] if images else 'NONE'})")

    # 3. 자격증명 + 클라이언트
    creds = get_credentials()
    mp = get_marketplace()
    seller_id = get_seller_id()
    print(f"\n▶ Marketplace: {mp.name} ({mp.marketplace_id})  Seller: {seller_id[:4]}...{seller_id[-4:]}")

    # 4. ProductType 조회 (읽기 전용 API)
    if not args.skip_api:
        print(f"\n▶ ProductType 검색 중... (keywords='{product['product_name'][:40]}')")
        try:
            ptd = ProductTypeDefinitions(credentials=creds, marketplace=mp)
            candidates = search_product_type(
                ptd,
                keywords=product["product_name"][:40],
                marketplace_id=mp.marketplace_id,
            )
            print(f"  후보 {len(candidates)}개:")
            for c in candidates[:10]:
                print(f"    - {c.get('name')}: {c.get('displayName')}")
            if candidates:
                recommended = candidates[0]["name"]
                print(f"  → 추천 productType: {recommended}")
                if args.product_type == "PRODUCT":
                    args.product_type = recommended
        except Exception as e:
            print(f"  ⚠️ search_definitions_product_types 실패: {e}")
            print(f"     → 기본값 '{args.product_type}' 사용")

    # 5. 페이로드 조립
    print(f"\n▶ productType: {args.product_type}")
    sku = f"{SKU_PREFIX}{args.product_id}"
    print(f"  SKU: {sku}")

    payload = build_payload(
        product=product,
        listing=listing,
        images=images,
        product_type=args.product_type,
        marketplace_id=mp.marketplace_id,
        seller_id=seller_id,
    )

    # 6a. 로컬 기본 검증
    v = validate_payload(payload, images)
    print(f"\n▶ Local Validation")
    print(f"  status  : {'✅ PASS' if v['ok'] else '❌ FAIL'}")
    if v["errors"]:
        print(f"  errors  ({len(v['errors'])}):")
        for e in v["errors"]:
            print(f"    ✗ {e}")
    if v["warnings"]:
        print(f"  warnings ({len(v['warnings'])}):")
        for w in v["warnings"]:
            print(f"    ! {w}")

    # 6b. Strict 스키마 검증 (optional)
    strict_result = None
    if args.strict:
        print(f"\n▶ Strict Schema Validation")
        try:
            client_ptd = ProductTypeDefinitions(credentials=creds, marketplace=mp)
            schema = fetch_product_type_schema(
                client_ptd, args.product_type, mp.marketplace_id,
            )
            print(f"  schema loaded: {len(json.dumps(schema))} bytes "
                  f"(cache: {SCHEMA_CACHE_DIR}/{args.product_type}.json)")
            strict_result = strict_validate(payload, schema)
            print(f"  required (top-level): {strict_result['schema_required']}")
            print(f"  status : {'✅ PASS' if strict_result['ok'] else '❌ FAIL'}")
            if strict_result["errors"]:
                print(f"  errors ({len(strict_result['errors'])}):")
                for e in strict_result["errors"]:
                    print(f"    ✗ {e}")
            if strict_result["warnings"]:
                print(f"  warnings ({len(strict_result['warnings'])}):")
                for w in strict_result["warnings"]:
                    print(f"    ! {w}")
        except Exception as e:
            print(f"  ❌ 스키마 검증 실패: {type(e).__name__}: {e}")

    # 7. 파일 저장
    output = args.output or f"/tmp/dryrun_payload_{args.product_id}.json"
    full = {
        "meta": {
            "product_id": args.product_id,
            "sku": sku,
            "marketplace_id": mp.marketplace_id,
            "seller_id": seller_id,
            "product_type": args.product_type,
            "image_count": len(images),
            "validation": v,
            "strict_validation": strict_result,
        },
        "request": {
            "endpoint": f"PUT /listings/2021-08-01/items/{seller_id}/{sku}",
            "query_params": {"marketplaceIds": mp.marketplace_id, "issueLocale": "en_US"},
            "body": payload,
        },
    }
    with open(output, "w", encoding="utf-8") as f:
        json.dump(full, f, indent=2, ensure_ascii=False)
    print(f"\n💾 Dry-run 페이로드 저장: {output}")
    print(f"   파일 크기: {os.path.getsize(output)} bytes")
    print(f"\n⛔ putListingsItem 호출 SKIP (Dry-Run 모드)")
    print(f"\n다음 단계:")
    print(f"  1. cat {output} | jq '.request.body.attributes | keys'")
    print(f"  2. cat {output} | jq '.meta.validation'")
    print(f"  3. 검증 통과 시 → Phase C-Sandbox 또는 Phase C-Prod 진행")

    return 0 if v["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
