"""기존 네이버 스마트스토어 등록 상품 일괄 수정.

수정 항목:
1. sellerTags — seo_tags → detailAttribute.seoInfo.sellerTags
2. name — 특수문자 치환 (" → 인치, * → x 등)
3. detailAttribute.naverShoppingSearchInfo — brandName/manufacturerName/modelName 보정
4. detailAttribute.productInfoProvidedNotice — manufacturer/modelName 보정
"""
import json
import logging
import os
import re
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# .env 로드
from pathlib import Path
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                os.environ.setdefault(k.strip(), v)

# ── 경로 설정
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "backend-shared"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.purchase.database import get_db
from backend.purchase.services.naver_commerce_service import (
    get_product, _get_token, _SESSION, BASE,
)

# ── 특수문자 치환 규칙 (네이버 금지: \ * ? " < >)
_SPECIAL_CHAR_MAP = {
    '"': '인치',
    '\u201c': '인치',
    '\u201d': '인치',
    '*': 'x',
    '\\': ' ',
    '?': ' ',
    '<': '(',
    '>': ')',
}
_SPECIAL_RE = re.compile('[' + re.escape(''.join(_SPECIAL_CHAR_MAP.keys())) + ']')


def clean_product_name(name: str) -> str:
    """네이버 금지 특수문자 치환 + 연속 공백 정리."""
    def _replace(m):
        return _SPECIAL_CHAR_MAP.get(m.group(0), ' ')
    cleaned = _SPECIAL_RE.sub(_replace, name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:100]


def extract_brand(name: str) -> str:
    """상품명 첫 단어(영문 2자 이상)를 브랜드명 후보로 추출."""
    words = name.split()
    if words and re.match(r'^[A-Za-z]', words[0]) and len(words[0]) >= 2:
        brand = words[0]
        if len(words) > 1 and re.match(r'^[A-Za-z]', words[1]) and len(words[1]) >= 2:
            brand = f"{words[0]} {words[1]}"
        return brand[:30]
    return "해외 브랜드"


def shorten_model_name(name: str, max_len: int = 50) -> str:
    """모델명을 간결하게 (50자 이내)."""
    if len(name) <= max_len:
        return name
    for sep in [',', ' - ', ' (', ' |']:
        idx = name.find(sep)
        if 10 < idx <= max_len:
            return name[:idx].strip()
    return name[:max_len].strip()


def build_seller_tags(seo_tags_json: str) -> list[dict]:
    """DB의 seo_tags JSON → 네이버 sellerTags 배열."""
    try:
        tags = json.loads(seo_tags_json) if seo_tags_json else []
    except (json.JSONDecodeError, TypeError):
        return []
    if not tags:
        return []
    # 네이버: 최대 10개, 공백 제거, 20자 이내
    valid = []
    for t in tags:
        t = t.strip().replace(" ", "")[:20]
        if t and len(t) >= 2:
            valid.append({"text": t})
    return valid[:10]


def _extract_restricted_words(response_body: dict) -> set[str]:
    """네이버 에러 응답에서 등록불가 단어를 추출."""
    words = set()
    for inp in response_body.get("invalidInputs") or []:
        msg = inp.get("message", "")
        # "태그 항목에 등록불가인 단어(BMW,반팔티)가 포함되어 있습니다."
        m = re.search(r"등록불가인 단어\(([^)]+)\)", msg)
        if m:
            for w in m.group(1).split(","):
                words.add(w.strip())
    return words


def _put_product(cpid: str, data: dict) -> tuple[bool, str]:
    """상품 PUT. 태그 거부 시 금지어만 제외하고 재시도 (최대 3회)."""
    token = _get_token()
    if not token:
        return False, "토큰 없음"

    for attempt in range(4):
        r = _SESSION.put(
            BASE + f"/v2/products/origin-products/{cpid}",
            json=data,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if r is None:
            return False, "no-response"
        if r.status_code < 400:
            note = f"태그{attempt}차필터" if attempt > 0 else ""
            return True, note

        try:
            body = r.json()
        except Exception:
            return False, f"{r.status_code}"

        # 태그 거부 에러인지 확인
        restricted = _extract_restricted_words(body)
        seo_info = data["originProduct"]["detailAttribute"].get("seoInfo", {})
        tags = seo_info.get("sellerTags", [])

        if restricted and tags:
            # 금지어가 포함된 태그만 제거
            filtered = [t for t in tags if t["text"] not in restricted]
            if filtered:
                seo_info["sellerTags"] = filtered
                logger.info(f"  금지어 {restricted} 제거 → {len(filtered)}개 태그로 재시도")
                time.sleep(0.5)
                continue
            else:
                # 태그 전부 금지 → seoInfo 제거 후 재시도
                del data["originProduct"]["detailAttribute"]["seoInfo"]
                logger.info(f"  태그 전부 금지 → 태그 없이 재시도")
                time.sleep(0.5)
                continue

        return False, f"{r.status_code} {r.text[:150]}"

    return False, "재시도 초과"


def main():
    dry_run = "--dry-run" in sys.argv
    limit = None
    for arg in sys.argv[1:]:
        if arg.startswith("--limit="):
            limit = int(arg.split("=")[1])

    with get_db() as conn:
        rows = conn.execute(
            """SELECT l.product_id, l.channel_product_id, p.title_ko, p.seo_tags, p.seo_title
               FROM listings_pa l
               JOIN products p ON p.id = l.product_id
               WHERE l.channel='smartstore' AND l.status='listed'
               AND l.channel_product_id IS NOT NULL AND l.channel_product_id != ''
               ORDER BY l.product_id"""
        ).fetchall()

    total = len(rows)
    if limit:
        rows = rows[:limit]
    logger.info(f"대상: {len(rows)}건 (전체 {total}건)")

    ok = fail = skip = tag_skipped = 0

    for i, row in enumerate(rows):
        pid = row["product_id"]
        cpid = row["channel_product_id"]
        title_ko = row["title_ko"] or ""
        seo_tags = row["seo_tags"] or "[]"

        # 1. 네이버에서 현재 데이터 조회
        current = get_product(cpid)
        if not current:
            logger.warning(f"[{i+1}/{len(rows)}] product {pid} 조회 실패 → 스킵")
            skip += 1
            continue

        op = current.get("originProduct", {})
        da = op.get("detailAttribute", {})
        nssi = da.get("naverShoppingSearchInfo", {})
        pin = da.get("productInfoProvidedNotice", {})
        etc = pin.get("etc", {})

        changes = []

        # 2. sellerTags 추가
        existing_seo = da.get("seoInfo", {})
        existing_tags = existing_seo.get("sellerTags") if existing_seo else None
        if not existing_tags:
            seller_tags = build_seller_tags(seo_tags)
            if seller_tags:
                da["seoInfo"] = {"sellerTags": seller_tags}
                changes.append(f"tags={len(seller_tags)}")

        # 3. 상품명 특수문자 치환
        old_name = op.get("name", "")
        new_name = clean_product_name(old_name)
        if new_name != old_name:
            op["name"] = new_name
            changes.append("name")

        # 4. 브랜드/제조사 보정
        brand = extract_brand(title_ko or new_name)
        if nssi.get("brandName") == "해외 브랜드" and brand != "해외 브랜드":
            nssi["brandName"] = brand
            changes.append(f"brand={brand}")
        if nssi.get("manufacturerName") == "해외 제조사" and brand != "해외 브랜드":
            nssi["manufacturerName"] = brand
            changes.append("mfr")

        # 5. 모델명 간결화
        short_model = shorten_model_name(new_name)
        if nssi.get("modelName") and len(nssi["modelName"]) > 50:
            nssi["modelName"] = short_model
            changes.append("model")

        # 6. 주요정보 보정
        if etc:
            if etc.get("manufacturer") == "상세설명 참조" and brand != "해외 브랜드":
                etc["manufacturer"] = brand
                changes.append("notice.mfr")
            if etc.get("modelName") and len(etc["modelName"]) > 50:
                etc["modelName"] = short_model
                changes.append("notice.model")
            if etc.get("itemName") and len(etc["itemName"]) > 50:
                etc["itemName"] = short_model
                changes.append("notice.item")

        if not changes:
            skip += 1
            continue

        if dry_run:
            logger.info(f"[{i+1}/{len(rows)}] product {pid}: {', '.join(changes)}")
            ok += 1
            continue

        # 7. PUT 업데이트
        success, note = _put_product(cpid, current)
        if success:
            ok += 1
            if note == "태그제외":
                tag_skipped += 1
            logger.info(f"[{i+1}/{len(rows)}] product {pid}: {', '.join(changes)}{' (태그제외)' if note else ''}")
        else:
            fail += 1
            logger.error(f"[{i+1}/{len(rows)}] product {pid} 실패: {note}")

        if (i + 1) % 100 == 0:
            logger.info(f"진행: {i+1}/{len(rows)} (성공 {ok}, 실패 {fail}, 스킵 {skip}, 태그제외 {tag_skipped})")

    logger.info(f"완료 — 성공 {ok}, 실패 {fail}, 스킵 {skip}, 태그거부 {tag_skipped} / 총 {len(rows)}")


if __name__ == "__main__":
    main()
