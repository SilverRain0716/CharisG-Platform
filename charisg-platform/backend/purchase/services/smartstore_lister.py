"""
smartstore_lister.py — 스마트스토어 리스팅 모듈.

products → 네이버 커머스 API 페이로드 변환 → 등록.
4/29 이후: customsDutyInfo 필수 (해외소싱 상품).

등록 전 완성 파이프라인: 상품명+이미지+속성+태그+브랜드를 모두 포함한 페이로드로 1회 등록.
"""
import hashlib
import json
import logging
import re
from typing import Optional

from backend.purchase.database import get_db
from backend.purchase.services import clean_policy
from backend.purchase.services.naver_commerce_service import register_product, upload_image, upload_images_batch

logger = logging.getLogger(__name__)

# ── 상품명/브랜드/태그 유틸 (naver_bulk_update.py에서 이식) ────

_SPECIAL_CHAR_MAP = {
    '"': '인치', '\u201c': '인치', '\u201d': '인치',
    '*': 'x', '\\': ' ', '?': ' ', '<': '(', '>': ')',
}
_SPECIAL_RE = re.compile('[' + re.escape(''.join(_SPECIAL_CHAR_MAP.keys())) + ']')


_BRAND_PLACEHOLDER_RE = re.compile(r'\[\s*브랜드[^\]]*\]\s*')


def _clean_product_name(name: str) -> str:
    """네이버 금지 특수문자 치환 + [브랜드명] placeholder 제거 + 50자 제한."""
    # AI 가 출력한 [브랜드명], [브랜드명 미포함] 같은 placeholder 제거
    name = _BRAND_PLACEHOLDER_RE.sub('', name or '')
    def _replace(m):
        return _SPECIAL_CHAR_MAP.get(m.group(0), ' ')
    cleaned = _SPECIAL_RE.sub(_replace, name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:50]


def _extract_brand(name: str) -> str:
    """상품명 첫 단어(영문 2자 이상)를 브랜드명 후보로 추출."""
    words = name.split()
    if words and re.match(r'^[A-Za-z]', words[0]) and len(words[0]) >= 2:
        brand = words[0]
        if len(words) > 1 and re.match(r'^[A-Za-z]', words[1]) and len(words[1]) >= 2:
            brand = f"{words[0]} {words[1]}"
        return brand[:30]
    return "해외 브랜드"


_TAG_DISALLOWED_RE = re.compile(r'[^가-힣A-Za-z0-9]')


def _build_seller_tags(seo_tags_json: str) -> list[dict]:
    """DB의 seo_tags JSON → 네이버 sellerTags 배열.

    네이버 제약: 한글·영숫자 외 문자 금지, 30byte(UTF-8) 이하, 최대 10개.
    """
    try:
        tags = json.loads(seo_tags_json) if seo_tags_json else []
    except (json.JSONDecodeError, TypeError):
        return []
    if not tags:
        return []
    valid = []
    for t in tags:
        if not isinstance(t, str):
            continue
        t = _TAG_DISALLOWED_RE.sub('', t.strip())
        while t and len(t.encode('utf-8')) > 30:
            t = t[:-1]
        if t and len(t) >= 2:
            valid.append({"text": t})
    return valid[:10]


def _sync_product_status(conn, product_id: int):
    """리스팅 채널 중 하나라도 listed/active이면 products.status를 listed로 승격."""
    row = conn.execute(
        """SELECT COUNT(*) c FROM listings_pa
           WHERE product_id=? AND status IN ('listed','active')""",
        (product_id,),
    ).fetchone()
    if row["c"] > 0:
        conn.execute(
            "UPDATE products SET status='listed' WHERE id=? AND status IN ('draft','ready')",
            (product_id,),
        )


def _upload_one_image_with_retry(local_path: str, retries: int = 3) -> Optional[str]:
    import time as _time
    for attempt in range(retries + 1):
        url = upload_image(local_path)
        if url:
            return url
        if attempt < retries:
            _time.sleep(2.0 * (attempt + 1))
    return None


def _compute_sha256(file_path: str) -> Optional[str]:
    """파일 SHA256 해시. 읽기 실패/빈 파일 시 None."""
    try:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        logger.warning(f"SHA256 계산 실패 {file_path}: {e}")
        return None


def _get_product_images(product_id: int) -> list[str]:
    """로컬 이미지를 네이버에 업로드. SHA256 + CDN URL 캐시로 재업로드 회피.

    흐름:
      1) image_cache 에서 product 이미지 10장 조회 (sha256, naver_cdn_url 포함)
      2) 각 이미지에 대해
         a. 자기 row 에 naver_cdn_url 있음 → 즉시 재사용
         b. 같은 sha256 의 다른 row 에 naver_cdn_url 있음 → 재사용 + 현재 row 에 저장
         c. 캐시 miss → 업로드 대상에 추가
      3) 업로드 대상만 배치 업로드 후 DB 저장
      4) image_idx 순으로 URL 리스트 반환
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, local_path, image_idx, sha256, naver_cdn_url
               FROM image_cache WHERE product_id=? ORDER BY image_idx LIMIT 10""",
            (product_id,),
        ).fetchall()
    if not rows:
        return []

    resolved: list[tuple[int, str]] = []  # (image_idx, url)
    upload_targets: list[tuple[int, int, str]] = []  # (cache_id, image_idx, local_path)
    cache_hits = 0

    with get_db() as conn:
        for r in rows:
            cache_id, idx, path, sha, cdn_url = r["id"], r["image_idx"], r["local_path"], r["sha256"], r["naver_cdn_url"]

            # (a) 이 row 에 이미 네이버 CDN URL 저장됨
            if cdn_url:
                resolved.append((idx, cdn_url))
                cache_hits += 1
                continue

            # sha256 없으면 계산 후 저장
            if not sha:
                sha = _compute_sha256(path)
                if sha:
                    conn.execute("UPDATE image_cache SET sha256=? WHERE id=?", (sha, cache_id))
                else:
                    upload_targets.append((cache_id, idx, path))
                    continue

            # (b) 다른 상품의 같은 이미지가 이미 네이버에 업로드됨 → URL 재사용
            cached = conn.execute(
                "SELECT naver_cdn_url FROM image_cache WHERE sha256=? AND naver_cdn_url IS NOT NULL LIMIT 1",
                (sha,),
            ).fetchone()
            if cached and cached["naver_cdn_url"]:
                reused_url = cached["naver_cdn_url"]
                resolved.append((idx, reused_url))
                conn.execute(
                    "UPDATE image_cache SET naver_cdn_url=?, naver_uploaded_at=CURRENT_TIMESTAMP WHERE id=?",
                    (reused_url, cache_id),
                )
                cache_hits += 1
                continue

            # (c) 캐시 miss
            upload_targets.append((cache_id, idx, path))

    # 업로드 대상이 있으면 배치 업로드
    if upload_targets:
        paths = [t[2] for t in upload_targets]
        results = upload_images_batch(paths)
        success_count = sum(1 for u in results if u)

        with get_db() as conn:
            # 해시도 아직 없는 행이 있을 수 있으니 각 성공 row 에 sha256 + URL 저장
            for (cache_id, idx, path), url in zip(upload_targets, results):
                if url:
                    resolved.append((idx, url))
                    sha = _compute_sha256(path)
                    if sha:
                        conn.execute(
                            "UPDATE image_cache SET sha256=?, naver_cdn_url=?, naver_uploaded_at=CURRENT_TIMESTAMP WHERE id=?",
                            (sha, url, cache_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE image_cache SET naver_cdn_url=?, naver_uploaded_at=CURRENT_TIMESTAMP WHERE id=?",
                            (url, cache_id),
                        )

        # 배치 전체 실패 시 대표이미지만 개별 폴백
        if success_count == 0:
            first_id, first_idx, first_path = upload_targets[0]
            url = _upload_one_image_with_retry(first_path)
            if url:
                resolved.append((first_idx, url))
                with get_db() as conn:
                    sha = _compute_sha256(first_path)
                    conn.execute(
                        "UPDATE image_cache SET sha256=COALESCE(sha256,?), naver_cdn_url=?, naver_uploaded_at=CURRENT_TIMESTAMP WHERE id=?",
                        (sha, url, first_id),
                    )
                logger.warning(f"[smartstore] product {product_id} 배치 실패 → 대표이미지 개별 업로드 성공")

    logger.info(
        f"[smartstore] product {product_id} 이미지 처리: "
        f"캐시 {cache_hits}/{len(rows)}, 업로드 {len(resolved) - cache_hits}/{len(upload_targets)}"
    )

    if not resolved:
        logger.error(f"[smartstore] product {product_id} 대표이미지 업로드 실패")
        return []

    resolved.sort(key=lambda x: x[0])
    return [url for _, url in resolved]


def preupload_images(product_id: int) -> list[str]:
    """이미지 사전 업로드 (파이프라인 Phase 1용). URL 목록 반환."""
    return _get_product_images(product_id)


def _validate_payload(name: str, price: int, category: str, detail_html: str) -> tuple[bool, str]:
    if not name or len(name) < 2:
        return False, "상품명이 너무 짧습니다 (최소 2자)"
    if len(name) > 50:
        return False, f"상품명이 50자를 초과합니다 ({len(name)}자)"
    if price < 1000:
        return False, f"판매가가 최소 금액(1,000원) 미만입니다 ({price}원)"
    if not category:
        return False, "카테고리 ID가 없습니다"
    if not category.isdigit() or not (6 <= len(category) <= 12):
        return False, f"카테고리 ID가 숫자 형식이 아닙니다 ({category[:30]})"
    if not detail_html or len(detail_html) < 10:
        return False, "상세페이지 HTML이 없거나 너무 짧습니다"
    return True, ""


def build_payload(product_id: int, image_urls: list[str] | None = None) -> Optional[dict]:
    with get_db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not p:
            return None
        listing = conn.execute(
            "SELECT sale_krw FROM listings_pa WHERE product_id=? AND channel='smartstore'",
            (product_id,),
        ).fetchone()
        detail = conn.execute(
            "SELECT html_content FROM detail_pages WHERE product_id=? ORDER BY updated_at DESC LIMIT 1",
            (product_id,),
        ).fetchone()

    raw_name = (p["title_ko"] or p["title_en"] or "").strip()
    name = _clean_product_name(raw_name)
    # ── [해외] 태그 자동 부여 (구매대행 표기) ──
    name = clean_policy.ensure_overseas_tag(name, max_len=50)
    price = int(listing["sale_krw"]) if listing and listing["sale_krw"] else int(p["sale_price_krw"] or 0)
    category = p["category_path"] or ""
    desc_html = detail["html_content"] if detail and detail["html_content"] else ""

    ok, err = _validate_payload(name, price, category, desc_html)
    if not ok:
        logger.warning(f"[smartstore] product {product_id} 검증 실패: {err}")
        return None

    if image_urls is None:
        image_urls = _get_product_images(product_id)

    if desc_html:
        local_pattern = re.compile(r'(?:http://[^"]*)?/api/pa/images/products/\d+/img_\d+\.jpg')
        local_matches = local_pattern.findall(desc_html)
        for i, local_url in enumerate(local_matches):
            if i < len(image_urls):
                desc_html = desc_html.replace(local_url, image_urls[i])
            else:
                desc_html = desc_html.replace(local_url, image_urls[0] if image_urls else "")
    images_payload = {}
    if image_urls:
        images_payload["representativeImage"] = {"url": image_urls[0]}
        if len(image_urls) > 1:
            images_payload["optionalImages"] = [{"url": u} for u in image_urls[1:9]]
    else:
        logger.warning(f"[smartstore] product {product_id}: 이미지 없음")
        images_payload["representativeImage"] = {"url": ""}

    # ── 브랜드/제조사 추출 ──
    brand = _extract_brand(raw_name)
    model_name = name[:50]

    # ── 태그 (sellerTags) ──
    seo_tags = p["seo_tags"] if p["seo_tags"] else "[]"
    seller_tags = _build_seller_tags(seo_tags)

    # ── 속성 (productAttributes) ──
    # naver_attributes_json: list[{attributeSeq, attributeValueSeq}]
    product_attributes = []
    naver_json = p["naver_attributes_json"] if "naver_attributes_json" in p.keys() else None
    if naver_json:
        try:
            parsed = json.loads(naver_json)
            if isinstance(parsed, list):
                product_attributes = [
                    a for a in parsed
                    if isinstance(a, dict)
                    and a.get("attributeSeq")
                    and a.get("attributeValueSeq")
                ]
        except (json.JSONDecodeError, TypeError):
            pass

    # ── detailAttribute 구성 ──
    detail_attribute = {
        "naverShoppingSearchInfo": {
            "modelName": model_name,
            "manufacturerName": brand,
            "brandName": brand,
            "catalogMatchingYn": False,
        },
        "afterServiceInfo": {
            "afterServiceTelephoneNumber": "010-8558-7277",
            "afterServiceGuideContent": "해외 구매대행 상품으로 국내 A/S가 불가합니다. 네이버 톡톡 또는 1:1 문의를 이용해주세요.",
        },
        "originAreaInfo": {
            "originAreaCode": "03",
            "content": "상세페이지 참고",
            "importer": "Charis G",
        },
        "taxType": "TAX",
        "minorPurchasable": True,
        "customsTaxType": "EXCLUDED",
        # 인증 면제 — 해외 구매대행 (어린이제품/KC/친환경 카테고리 등록 시 필수)
        # 2026-04-28 추가: 어린이제품 인증대상/KC 인증대상 카테고리 187건 거부 fix.
        # commerce-api Discussion #704 기반 페이로드.
        "certificationTargetExcludeContent": {
            "childCertifiedProductExclusionYn": True,
            "kcCertifiedProductExclusionYn": "KC_EXEMPTION_OBJECT",
            "kcExemptionType": "OVERSEAS",
            "greenCertifiedProductExclusionYn": True,
        },
        "productInfoProvidedNotice": {
            "productInfoProvidedNoticeType": "ETC",
            "etc": {
                "returnCostReason": "네이버 톡톡 또는 1:1 문의",
                "noRefundReason": "네이버 톡톡 또는 1:1 문의",
                "qualityAssuranceStandard": "제조사/수입사 품질보증 기준에 따름",
                "compensationProcedure": "전자상거래 등에서의 소비자보호에 관한 법률에 따름",
                "troubleShootingContents": "네이버 톡톡 또는 1:1 문의",
                "itemName": model_name,
                "modelName": model_name,
                "manufacturer": brand,
                "customerServicePhoneNumber": "010-8558-7277",
            },
        },
    }

    if seller_tags:
        detail_attribute["seoInfo"] = {"sellerTags": seller_tags}

    if product_attributes:
        detail_attribute["productAttributes"] = product_attributes

    payload = {
        "originProduct": {
            "statusType": "SALE",
            "name": name,
            "salePrice": price,
            "stockQuantity": 100,
            "leafCategoryId": category,
            "detailContent": desc_html,
            "images": images_payload,
            "deliveryInfo": {
                "deliveryType": "DELIVERY",
                "deliveryAttributeType": "NORMAL",
                "deliveryCompany": "CJGLS",
                "deliveryBundleGroupUsable": True,
                "deliveryBundleGroupId": 57248768,
                "deliveryFee": {
                    "deliveryFeeType": "FREE",
                },
                "claimDeliveryInfo": {
                    "returnDeliveryCompanyPriorityType": "PRIMARY",
                    "returnDeliveryFee": 5000,
                    "exchangeDeliveryFee": 5000,
                    "shippingAddressId": 200297709,
                    "returnAddressId": 200335116,
                    "freeReturnInsuranceYn": False,
                },
            },
            "detailAttribute": detail_attribute,
        },
        "smartstoreChannelProduct": {
            "channelProductDisplayStatusType": "ON",
            "naverShoppingRegistration": True,
        },
    }
    return payload


def list_product(product_id: int, image_urls: list[str] | None = None) -> dict:
    with get_db() as conn:
        existing = conn.execute(
            """SELECT channel_product_id FROM listings_pa
               WHERE product_id=? AND channel='smartstore'""",
            (product_id,),
        ).fetchone()
    if existing and existing["channel_product_id"]:
        return {"ok": False, "skip": True,
                "error": f"이미 등록됨 (channel_product_id={existing['channel_product_id']})"}

    # ── 클린 정책 검사 (중복 ASIN + 금지 성분) ──
    with get_db() as conn:
        prow = conn.execute(
            "SELECT asin, title_en, title_ko, category_path FROM products WHERE id=?",
            (product_id,),
        ).fetchone()
    if prow:
        # 1) 중복 ASIN
        asin = prow["asin"]
        if asin:
            is_dup, dup_info = clean_policy.check_duplicate_asin(asin, channel='smartstore', exclude_product_id=product_id)
            if is_dup:
                reason = f"중복 ASIN — 이미 listed (product_id={dup_info['product_id']}, cpid={dup_info['channel_product_id']})"
                with get_db() as conn:
                    conn.execute(
                        """UPDATE listings_pa SET status='excluded',
                           error_message=?, last_synced_at=CURRENT_TIMESTAMP
                           WHERE product_id=? AND channel='smartstore'""",
                        (reason, product_id),
                    )
                clean_policy.log_violation(
                    stage='upload_smartstore', violation_type='duplicate_asin',
                    action_taken='excluded', asin=asin,
                    product_id=product_id, channel='smartstore',
                    notes=f'기존 listed product_id={dup_info["product_id"]}',
                )
                return {"ok": False, "skip": True, "error": reason}

        # 2) 금지 성분
        blocked_ing, ing = clean_policy.check_prohibited_ingredients(
            prow["title_en"] or "", prow["title_ko"] or "",
        )
        if blocked_ing:
            reason = f"금지 성분 차단 ({ing}) — 국내 의약품 분류 또는 수입금지"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='smartstore'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_smartstore', violation_type='prohibited_ingredient',
                action_taken='excluded', matched_keyword=ing,
                product_id=product_id, channel='smartstore',
                original_text=prow['title_en'],
            )
            return {"ok": False, "skip": True, "error": reason}

        # 3) 취급불가 카테고리
        blocked_cat, cat_kw = clean_policy.check_prohibited_category(prow["category_path"] or "")
        if blocked_cat:
            reason = f"취급불가 카테고리 ({cat_kw})"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='smartstore'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_smartstore', violation_type='prohibited_category',
                action_taken='excluded', matched_keyword=cat_kw,
                product_id=product_id, channel='smartstore',
                original_text=prow['category_path'],
            )
            return {"ok": False, "skip": True, "error": reason}

    payload = build_payload(product_id, image_urls=image_urls)
    if not payload:
        err = f"payload build 실패 (검증 오류 또는 상품 없음)"
        with get_db() as conn:
            conn.execute(
                """UPDATE listings_pa SET status='excluded', error_message=?,
                   last_synced_at=CURRENT_TIMESTAMP
                   WHERE product_id=? AND channel='smartstore'""",
                (err, product_id),
            )
        return {"ok": False, "error": err}
    result = register_product(payload)
    if not result:
        err = "naver api 호출 실패 (응답 없음)"
        with get_db() as conn:
            conn.execute(
                """UPDATE listings_pa SET status='excluded', error_message=?,
                   last_synced_at=CURRENT_TIMESTAMP
                   WHERE product_id=? AND channel='smartstore'""",
                (err, product_id),
            )
        return {"ok": False, "error": err}

    if result.get("_error"):
        err = result["_error"]
        with get_db() as conn:
            conn.execute(
                """UPDATE listings_pa SET status='excluded', error_message=?,
                   last_synced_at=CURRENT_TIMESTAMP
                   WHERE product_id=? AND channel='smartstore'""",
                (err, product_id),
            )
        return {"ok": False, "error": err}

    if result.get("_skip"):
        with get_db() as conn:
            conn.execute(
                """UPDATE listings_pa SET status='excluded', error_message=?,
                   last_synced_at=CURRENT_TIMESTAMP
                   WHERE product_id=? AND channel='smartstore'""",
                (result["_skip"], product_id),
            )
        return {"ok": False, "skip": True, "error": result["_skip"]}

    with get_db() as conn:
        conn.execute(
            """UPDATE listings_pa SET channel_product_id=?, status='listed',
               last_synced_at=CURRENT_TIMESTAMP
               WHERE product_id=? AND channel='smartstore'""",
            (str(result.get("originProductNo", "")), product_id),
        )
        # 등록 페이로드에 inferred attributes를 포함했다면 batch-all 중복 처리 방지를 위해 마킹
        if payload.get("originProduct", {}).get("detailAttribute", {}).get("productAttributes"):
            conn.execute(
                "UPDATE products SET attributes_updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (product_id,),
            )
        _sync_product_status(conn, product_id)
    return {"ok": True, "result": result}
