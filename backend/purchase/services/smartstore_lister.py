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


# ── 주소록 ID (계정별) ─────────────────────────────────────────
# 구계정 기본값 = 기존 하드코딩 값(회귀 0). 신계정은 .env NAVER_NEW_* 로 주입.
_ADDR_DEFAULT = {"SHIPPING_ADDRESS_ID": "200297709", "RETURN_ADDRESS_ID": "200335116"}


def _addr_id(name: str) -> int:
    from backend.purchase.services.naver_commerce_service import active_account
    from backend_shared._config import naver_cfg
    v = naver_cfg(name, active_account()) or _ADDR_DEFAULT[name]
    return int(str(v).strip())


def _shipping_address_id() -> int:
    return _addr_id("SHIPPING_ADDRESS_ID")


def _return_address_id() -> int:
    return _addr_id("RETURN_ADDRESS_ID")


_BUNDLE_GROUP_BY_ACCT = {"old": 57248768}   # 신계정은 미확보 → 묶음배송 미사용


def _bundle_group() -> dict:
    """계정별 묶음배송그룹 설정. 미보유 계정은 사용안함으로 내려 400 을 피한다."""
    from backend.purchase.services.naver_commerce_service import active_account
    gid = _BUNDLE_GROUP_BY_ACCT.get(active_account())
    if gid:
        return {"deliveryBundleGroupUsable": True, "deliveryBundleGroupId": gid}
    return {"deliveryBundleGroupUsable": False}


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


def build_payload(product_id: int, image_urls: list[str] | None = None,
                  status_type: str = "SALE") -> Optional[dict]:
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
        # ★파일명에 내용 해시가 붙는다(캐시 무효화, 2026-08-12): img_000.7bba1514.jpg
        #   그리고 상세는 agent_sec{i}.<hash>.jpg 를 쓴다. 둘 다 잡아야 네이버 CDN 으로 치환된다.
        #   종전 패턴(img_\d+\.jpg)은 해시가 붙으면서 하나도 안 걸렸다.
        local_pattern = re.compile(
            r'(?:https?://[^"]*)?/api/pa/images/products/\d+/'
            r'(?:img_\d+|agent_sec\d+|agent_[a-z]+_sec\d+)(?:\.[0-9a-f]{6,})?\.jpe?g')
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
        except (json.JSONDecodeError, TypeError) as e:
            # 2026-05-19: silent skip 위험 — 속성 누락 시 검색 누락 → 매출 손실
            logger.warning(
                f"build_payload: naver_attributes_json parse 실패 pid={product_id} "
                f"err={e} raw[:80]={(naver_json or '')[:80]!r}"
            )

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

    # ★2026-08-02: 샴푸/세제 등 가격표시제 대상 카테고리는 unitCapacity.unitPriceYn 이 필수다.
    #   누락 시 "단위가격 사용여부를 선택해주세요" 400. 대상이 아니면 무시되므로 항상 넣어도 안전.
    detail_attribute.setdefault("unitCapacity", {"unitPriceYn": False})
    detail_attribute.setdefault("unitQuantity", {"unitPriceYn": False})

    payload = {
        "originProduct": {
            # ★네이버엔 임시저장이 없다(도식 M19a). 검증 등록은 SUSPENSION 으로 올려 노출을 막는다.
            "statusType": status_type,
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
                # ★2026-08-02: 묶음배송그룹 ID 는 계정별로 다르다. 57248768 은 구계정 전용이라
                #   신계정에 그대로 보내면 "묶음배송그룹 항목이 유효하지 않습니다" 400.
                #   신계정 그룹 ID 는 조회 API 가 없어(엔드포인트 404) 미사용으로 등록한다.
                #   배송비가 FREE 라 묶음 여부가 실익에 영향이 없다. ID 확보 시 _bundle_group() 에 채우면 된다.
                **_bundle_group(),
                "deliveryFee": {
                    "deliveryFeeType": "FREE",
                },
                "claimDeliveryInfo": {
                    "returnDeliveryCompanyPriorityType": "PRIMARY",
                    "returnDeliveryFee": 5000,
                    "exchangeDeliveryFee": 5000,
                    "shippingAddressId": _shipping_address_id(),
                    "returnAddressId": _return_address_id(),
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
            "SELECT asin, title_en, title_ko, category_path, brand, amazon_manufacturer, "
            "sp_manufacturer FROM products WHERE id=?",
            (product_id,),
        ).fetchone()
    if prow:
        # 0) ★2026-08-03 카테고리 게이트 — 네이버는 화장품/식품 전용.
        #    그룹 등록이 부모의 미배정 형제를 끌고와 오염되는 것을 등록 직전에 차단한다.
        try:
            with get_db() as _gc:
                _cat = _gc.execute(
                    "SELECT amazon_category_json, sp_product_type, sp_browse_classification, "
                    "sp_website_display_group, title_ko, title_en FROM products WHERE id=?", (product_id,)
                ).fetchone()
            from backend.purchase.services.naver_commerce_service import active_account as _na
            _ok, _why, _cls = clean_policy.check_naver_gate_by_product(_cat, _na())
            if not _ok:
                logger.info(f"[naver-gate] product {product_id} 차단 — {_why}")
                return {"ok": False, "skip": True, "error": f"카테고리 게이트: {_why}"}
        except Exception as _ge:
            logger.warning(f"[naver-gate] product {product_id} 검사 실패(통과): {_ge}")

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

        # 1-1) 브랜드 블랙리스트(정품 게이팅 + 지재권 함정) — 쿠팡과 동일 필터/동일 settings 공유 (2026-07-02)
        from backend.purchase.services.coupang_lister import _is_brand_blocked, _load_brand_blocklist
        _bl_match = _is_brand_blocked(prow["title_en"] or "", prow["title_ko"] or "", _load_brand_blocklist())
        # ★삭제이력 ASIN 재등록 차단(2026-08-05)
        if not _bl_match:
            try:
                from backend.purchase.services import clean_policy as _cp2
                _ab, _ar = _cp2.check_blocked_asin(prow["asin"] or "")
                if _ab:
                    _bl_match = _ar
            except Exception:
                pass

        # ★브랜드필드 차단(2026-08-05) — coupang_lister 와 동일 정책
        if not _bl_match:
            try:
                from backend.purchase.services import clean_policy as _cp
                _b, _r = _cp.check_brand_field_blocked(prow["brand"] or "")
                if _b:
                    _bl_match = _r
            except Exception:
                pass
        if _bl_match:
            reason = f"브랜드 블랙리스트 차단 ({_bl_match}) — 정품 게이팅/지재권 침해 위험"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='smartstore'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_smartstore', violation_type='brand_blocklist',
                action_taken='excluded', matched_keyword=_bl_match,
                product_id=product_id, channel='smartstore',
                original_text=prow['title_ko'] or prow['title_en'],
            )
            return {"ok": False, "skip": True, "error": reason}

        # 1-1b) IP/총판 브랜드 차단 (화장품/건기식 전용, 2026-07-23) — 쿠팡과 동일 함수/watchlist 공유
        from backend.purchase.services.coupang_lister import check_ip_brand_blocked
        _ip_brand = check_ip_brand_blocked(
            prow["category_path"], prow["title_en"] or "", prow["title_ko"] or "", prow["brand"] or "")
        if _ip_brand:
            reason = f"IP총판브랜드 차단 ({_ip_brand}) — 화장품/건기식 지재권"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='smartstore'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_smartstore', violation_type='ip_watchlist_brand',
                action_taken='excluded', matched_keyword=_ip_brand,
                product_id=product_id, channel='smartstore',
                original_text=prow['title_ko'] or prow['title_en'],
            )
            return {"ok": False, "skip": True, "error": reason}

        # 1-2) 한국 manufacturer 게이트 (IP 라이선스 보호) — 쿠팡과 동일 (2026-07-02)
        _mfr = ((prow["amazon_manufacturer"] or "").strip()
                or (prow["sp_manufacturer"] or "").strip() or None)
        _kr_blocked, _kr_reason = clean_policy.check_korean_manufacturer(_mfr)
        if _kr_blocked:
            reason = f"한국 manufacturer 차단 ({_mfr}) — IP 라이선스 보호 [{_kr_reason}]"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='smartstore'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_smartstore', violation_type='korean_manufacturer',
                action_taken='excluded', matched_keyword=_mfr,
                product_id=product_id, channel='smartstore', asin=prow['asin'],
                notes=_kr_reason,
            )
            return {"ok": False, "skip": True, "error": reason}

        # 1-3) 전기용품 차단 (KC 전기안전인증) — 쿠팡과 동일
        _el_blocked, _el_kw = clean_policy.check_electric_appliance(
            prow["title_en"] or "", prow["title_ko"] or "",
        )
        if _el_blocked:
            reason = f"전기용품 차단 ({_el_kw}) — KC 전기안전인증"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='smartstore'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_smartstore', violation_type='electric_appliance',
                action_taken='excluded', matched_keyword=_el_kw,
                product_id=product_id, channel='smartstore', asin=prow['asin'],
                notes='KC 전기안전인증 필요',
            )
            return {"ok": False, "skip": True, "error": reason}

        # 1-3b) 거울/벽걸이 등 취급제외 카테고리 차단 (2026-07-05) — 쿠팡과 동일
        _ec_blocked, _ec_kw = clean_policy.check_excluded_amazon_category(product_id=product_id)
        if _ec_blocked:
            reason = f"취급제외 카테고리 차단 ({_ec_kw}) — 거울/벽걸이(파손·대형 리스크)"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='smartstore'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_smartstore', violation_type='excluded_category',
                action_taken='excluded', matched_keyword=_ec_kw,
                product_id=product_id, channel='smartstore', asin=prow['asin'],
                notes='거울/벽걸이 취급제외',
            )
            return {"ok": False, "skip": True, "error": reason}

        # 1-4) 목록통관 면세 한도($150) 차단 — 쿠팡과 동일 임계값 공유
        from backend.purchase.services.coupang_lister import _exceeds_customs_limit, CUSTOMS_DUTY_FREE_USD
        with get_db() as conn:
            _cr = conn.execute("SELECT cost_usd FROM products WHERE id=?", (product_id,)).fetchone()
        if _exceeds_customs_limit(_cr["cost_usd"] if _cr else None):
            _cu = float(_cr["cost_usd"])
            reason = f"관세 한도 초과 차단 (원가 ${_cu:.2f} > ${int(CUSTOMS_DUTY_FREE_USD)}) — 목록통관 면세한도 초과로 관세 발생"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='smartstore'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_smartstore', violation_type='customs_over_limit',
                action_taken='excluded', matched_keyword=f"${_cu:.2f}",
                product_id=product_id, channel='smartstore', asin=prow['asin'],
                notes='목록통관 면세한도($150) 초과',
            )
            return {"ok": False, "skip": True, "error": reason}

        # 1-5) 의약외품 차단 (약사법) — 쿠팡과 동일
        _qd_blocked, _qd_kw = clean_policy.check_quasi_drug(prow["title_ko"], prow["title_en"])
        if _qd_blocked:
            reason = f"의약외품 차단 ({_qd_kw}) — 약사법 무허가 의약외품"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='smartstore'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_smartstore', violation_type='quasi_drug',
                action_taken='excluded', matched_keyword=_qd_kw,
                product_id=product_id, channel='smartstore',
                original_text=prow['title_ko'],
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

        # 2-1) DTC 유전자검사 키트 영구 차단 (생명윤리법 제49조1항)
        gk_blocked, gk_kw = clean_policy.check_prohibited_genetic_kit(
            prow["title_en"], prow["title_ko"]
        )
        if gk_blocked:
            reason = f"DTC 유전자검사 키트 차단 ({gk_kw}) — 생명윤리법 제49조1항 위반"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='smartstore'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_smartstore', violation_type='dtc_genetic_kit',
                action_taken='excluded', matched_keyword=gk_kw,
                product_id=product_id, channel='smartstore',
                original_text=prow['title_en'],
            )
            return {"ok": False, "skip": True, "error": reason}

        # 2-2) 의류·신발 임시 차단 (PA_DISABLE_APPAREL_SHOES_BLOCK=1 로 해제)
        ap_blocked, ap_kw = clean_policy.check_blocked_apparel_shoes(prow["title_ko"])
        if ap_blocked:
            reason = f"의류·신발 임시 차단 ({ap_kw}) — 사장님 별도 지시 전까지"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='smartstore'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_smartstore', violation_type='apparel_shoes_blocked',
                action_taken='excluded', matched_keyword=ap_kw,
                product_id=product_id, channel='smartstore',
                original_text=prow['title_ko'],
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

        # 4) KC 비면제 품목 차단 (KC마크 없이 구매대행 불가)
        cpc = None  # 어린이제품 검출용 — product 의 쿠팡 매핑 카테고리 역참조
        with get_db() as conn:
            crow = conn.execute(
                "SELECT coupang_category_code FROM listings_pa "
                "WHERE product_id=? AND channel='coupang' "
                "AND coupang_category_code IS NOT NULL LIMIT 1",
                (product_id,),
            ).fetchone()
        if crow:
            cpc = crow["coupang_category_code"]
        kc_blocked, kc_reason = clean_policy.check_kc_blocked(
            prow["title_en"] or "", prow["title_ko"] or "", coupang_category_code=cpc,
            brand=(prow["brand"] or ""),
        )
        if kc_blocked:
            reason = f"KC 비면제 품목 차단 ({kc_reason}) — KC마크 없이 구매대행 불가"
            with get_db() as conn:
                conn.execute(
                    """UPDATE listings_pa SET status='excluded',
                       error_message=?, last_synced_at=CURRENT_TIMESTAMP
                       WHERE product_id=? AND channel='smartstore'""",
                    (reason, product_id),
                )
            clean_policy.log_violation(
                stage='upload_smartstore', violation_type='kc_required',
                action_taken='excluded', matched_keyword=kc_reason,
                product_id=product_id, channel='smartstore', asin=prow['asin'],
                original_text=prow['title_en'],
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
        # ★2026-08-02: UPDATE 만 하면 listings_pa 에 행이 없는 신규상품은 0행 갱신으로
        #   네이버엔 등록됐는데 DB 는 모르는 '무추적' 상태가 된다(실측: 시범등록 3건 전부).
        #   행이 없으면 INSERT 한다.
        cpid = str(result.get("originProductNo", ""))
        cur = conn.execute(
            """UPDATE listings_pa SET channel_product_id=?, status='listed',
               last_synced_at=CURRENT_TIMESTAMP
               WHERE product_id=? AND channel='smartstore'""",
            (cpid, product_id),
        )
        if not cur.rowcount:
            from backend.purchase.services.naver_commerce_service import active_account
            _p = conn.execute("SELECT sale_price_krw FROM products WHERE id=?", (product_id,)).fetchone()
            conn.execute(
                """INSERT INTO listings_pa
                     (product_id, channel, channel_product_id, status, naver_account,
                      sale_krw, created_at, last_synced_at, acct_key)
                   VALUES (?, 'smartstore', ?, 'listed', ?, ?,
                           CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)""",
                (product_id, cpid, active_account(),
                 int((_p["sale_price_krw"] if _p else 0) or 0),
                 active_account() or ""),
            )
        # 등록 페이로드에 inferred attributes를 포함했다면 batch-all 중복 처리 방지를 위해 마킹
        if payload.get("originProduct", {}).get("detailAttribute", {}).get("productAttributes"):
            conn.execute(
                "UPDATE products SET attributes_updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (product_id,),
            )
        _sync_product_status(conn, product_id)
    return {"ok": True, "result": result}
