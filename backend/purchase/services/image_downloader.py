"""PA 이미지 다운로더 — Amazon 이미지 → EC2 로컬 저장 + 자동 삭제 관리.

이미지 수집 우선순위:
  1. SP-API Catalog Items (안정적, 공식 API)
  2. Amazon 페이지 크롤링 (fallback)

삭제 정책:
  - 채널 업로드 완료 시 → 즉시 삭제 예약
  - 미등록 → 30일(settings.image_retention_days) 후 자동 삭제
"""
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from io import BytesIO
from PIL import Image as _PIL_Image, ImageOps as _PIL_ImageOps

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)

MEDIA_ROOT = Path(os.environ.get(
    "PA_MEDIA_ROOT",
    str(Path(__file__).resolve().parent.parent / "media"),
))
IMAGES_DIR = MEDIA_ROOT / "products"

DEFAULT_RETENTION_DAYS = 30

# ── 쿠팡 이미지 규격 보정 ──────────────────────────────────────
# 쿠팡은 대표/기타 모든 이미지가 양변 ≥500px(권장 1:1), ≤5000px, ≤10MB 여야 함.
# Amazon 원본이 길쭉하면 thumbnail 후 단변<500 이 되어 쿠팡이 승인반려.
# (2026-06-01 업로드 458건 중 97%가 이 사유, 87%는 기타이미지 단변<500.)
# → 모든 이미지를 흰배경 1:1 1000x1000 으로 리사이즈+패딩해 규격을 강제 보장.
_COUPANG_IMG_SIDE = 1000


def _normalize_for_coupang(img: "_PIL_Image.Image") -> "_PIL_Image.Image":
    """이미지를 쿠팡 규격(양변 ≥500, 1:1 권장)에 맞춰 흰배경 정사각 캔버스로 보정.

    - EXIF 회전 보정 → RGB 변환 → 1000x1000 fit(업/다운스케일) + 흰배경 패딩.
    - 결과는 항상 1000x1000 이므로 양변≥500·≤5000 규격을 무조건 충족.
    """
    img = _PIL_ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return _PIL_ImageOps.pad(
        img,
        (_COUPANG_IMG_SIDE, _COUPANG_IMG_SIDE),
        method=_PIL_Image.LANCZOS,
        color=(255, 255, 255),
        centering=(0.5, 0.5),
    )


_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_retention_days() -> int:
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key='image_retention_days'"
            ).fetchone()
            return int(row["value"]) if row else DEFAULT_RETENTION_DAYS
    except Exception:
        return DEFAULT_RETENTION_DAYS


# ── SP-API 이미지 수집 (primary) ─────────────────────

_PAT_SP_IMG_ID = re.compile(r'/I/([A-Za-z0-9+_%-]+?)(?:\._[^/]*)?\.jpg')


def fetch_amazon_images_sp_api(asin: str, max_images: int = 15) -> list[str]:
    """SP-API Catalog Items로 이미지 URL 수집 (hiRes 우선).

    Returns: 이미지 URL 리스트 (최대 max_images개, 중복 제거, 큰 해상도 우선).
    """
    try:
        from sp_api.api import CatalogItems
        from sp_api.base import Marketplaces
        from backend.dropshipping.services.amazon_sp_api_service import get_credentials
    except ImportError:
        logger.warning("sp_api 모듈 없음 — SP-API 이미지 수집 불가")
        return []

    try:
        creds = get_credentials()
        catalog = CatalogItems(credentials=creds, marketplace=Marketplaces.US)
        resp = catalog.get_catalog_item(
            asin=asin,
            includedData="images",
            marketplaceIds=["ATVPDKIKX0DER"],
        )
        item = resp.payload
        image_sets = item.get("images", [])
        if not image_sets:
            return []

        # MAIN variant 우선
        main_set = image_sets[0]
        for s in image_sets:
            if s.get("variant") == "MAIN":
                main_set = s
                break

        raw_images = main_set.get("images", [])

        # 이미지 ID별 가장 큰 해상도만 선택
        best_by_id: dict[str, tuple[int, str]] = {}
        for img in raw_images:
            url = img.get("link", "")
            w = img.get("width", 0)
            h = img.get("height", 0)
            area = w * h
            m = _PAT_SP_IMG_ID.search(url)
            img_id = m.group(1) if m else url
            if img_id not in best_by_id or area > best_by_id[img_id][0]:
                best_by_id[img_id] = (area, url)

        sorted_imgs = sorted(best_by_id.values(), key=lambda x: -x[0])
        result = [url for _, url in sorted_imgs[:max_images]]
        logger.info(f"🔍 SP-API {asin}: {len(result)}장 이미지 수집")
        return result

    except Exception as e:
        logger.warning(f"SP-API 이미지 수집 실패 ({asin}): {e}")
        return []


def fetch_product_info_sp_api(asin: str) -> dict:
    """SP-API로 상품 정보 수집.

    Returns dict with keys:
      title, brand, description, bullet_points, images,
      amazon_price_usd, dimensions, identifiers, classifications
    실패 시 빈 dict.
    """
    try:
        from sp_api.api import CatalogItems
        from sp_api.base import Marketplaces
        from backend.dropshipping.services.amazon_sp_api_service import get_credentials
    except ImportError:
        return {}

    try:
        creds = get_credentials()
        catalog = CatalogItems(credentials=creds, marketplace=Marketplaces.US, version="2022-04-01")
        resp = catalog.get_catalog_item(
            asin=asin,
            includedData="summaries,attributes,images,dimensions,identifiers,classifications,relationships",
            marketplaceIds=["ATVPDKIKX0DER"],
        )
        item = resp.payload
        result: dict = {}

        # ── summaries → title, brand, amazon_price_usd ──
        summaries = item.get("summaries", [])
        if summaries:
            s = summaries[0]
            result["title"] = s.get("itemName", "")
            result["brand"] = s.get("brand", "")

        # ── attributes → description, bullet_point, list_price ──
        attrs = item.get("attributes", {})
        if attrs:
            bullets = attrs.get("bullet_point", [])
            if bullets:
                result["bullet_points"] = [
                    b.get("value", "") for b in bullets if b.get("value")
                ]
            descs = attrs.get("product_description", [])
            if descs:
                result["description"] = descs[0].get("value", "")
            # 판매가: list_price (v2022-04-01: [{currency, value, marketplace_id}])
            list_prices = attrs.get("list_price", [])
            if list_prices:
                lp = list_prices[0]
                # v2022-04-01: value 가 바로 숫자 / v2020-12-01: value.amount
                amt = lp.get("value")
                if isinstance(amt, dict):
                    amt = amt.get("amount")
                if amt is not None:
                    try:
                        result["amazon_price_usd"] = float(amt)
                    except (ValueError, TypeError):
                        pass

        # ── images ──
        image_sets = item.get("images", [])
        if image_sets:
            main_set = image_sets[0]
            for s in image_sets:
                if s.get("variant") == "MAIN":
                    main_set = s
                    break
            raw = main_set.get("images", [])
            best_by_id: dict[str, tuple[int, str]] = {}
            for img in raw:
                url = img.get("link", "")
                area = img.get("width", 0) * img.get("height", 0)
                m = _PAT_SP_IMG_ID.search(url)
                img_id = m.group(1) if m else url
                if img_id not in best_by_id or area > best_by_id[img_id][0]:
                    best_by_id[img_id] = (area, url)
            sorted_imgs = sorted(best_by_id.values(), key=lambda x: -x[0])
            result["images"] = [url for _, url in sorted_imgs[:15]]

        # ── dimensions → {length, width, height, weight} ──
        dim_sets = item.get("dimensions", [])
        if dim_sets:
            d = dim_sets[0]  # 첫 번째 marketplace 데이터
            dims: dict = {}
            pkg = d.get("package", {})
            itm = d.get("item", {})
            # item dimensions 우선, 없으면 package
            src = itm if itm else pkg
            for key in ("length", "width", "height"):
                v = src.get(key)
                if v:
                    dims[key] = v.get("value")
                    dims[f"{key}_unit"] = v.get("unit", "")
            # weight: item weight 우선
            w = itm.get("weight") or pkg.get("weight")
            if w:
                dims["weight"] = w.get("value")
                dims["weight_unit"] = w.get("unit", "")
            if dims:
                result["dimensions"] = dims

        # ── identifiers → [{type, value}, ...] ──
        id_sets = item.get("identifiers", [])
        if id_sets:
            ids: list[dict] = []
            for id_set in id_sets:
                for ident in id_set.get("identifiers", []):
                    id_type = ident.get("identifierType", "")
                    id_val = ident.get("identifier", "")
                    if id_type and id_val:
                        ids.append({"type": id_type, "value": id_val})
            if ids:
                # 중복 제거 (type+value 기준)
                seen = set()
                unique_ids = []
                for i in ids:
                    k = (i["type"], i["value"])
                    if k not in seen:
                        seen.add(k)
                        unique_ids.append(i)
                result["identifiers"] = unique_ids

        # ── classifications → [{nodeId, name, path}, ...] ──
        cls_sets = item.get("classifications", [])
        if cls_sets:
            nodes: list[dict] = []
            for cls in cls_sets:
                for cat in cls.get("classifications", []):
                    node_id = cat.get("classificationId", "")
                    name = cat.get("displayName", "")
                    if node_id or name:
                        nodes.append({"nodeId": node_id, "name": name})
            if nodes:
                result["classifications"] = nodes

        # ── relationships → 변형 발굴 (parent_asin / is_parent) ──
        # SP-API 규칙: VARIATION 관계에서
        #   childAsins 보유  → 쿼리 ASIN = 부모 (케이스 2)
        #   parentAsins 보유 → 쿼리 ASIN = 자식 (케이스 3)
        #   둘 다 없음        → 단독 (케이스 1)
        # parent_asin 한 컬럼으로 3케이스 표현: 단독=None / 부모=self / 자식=부모ASIN
        rel_sets = item.get("relationships", [])
        for rel_set in rel_sets:
            for rel in rel_set.get("relationships", []):
                if rel.get("type") != "VARIATION":
                    continue
                child_asins = rel.get("childAsins") or []
                parent_asins = rel.get("parentAsins") or []
                if child_asins:
                    result["is_parent"] = True
                    result["parent_asin"] = asin          # self = 패밀리 키
                    result["child_asins"] = child_asins
                elif parent_asins:
                    result["is_parent"] = False
                    result["parent_asin"] = parent_asins[0]
                break
            if "parent_asin" in result:
                break

        return result

    except Exception as e:
        logger.warning(f"SP-API 상품정보 수집 실패 ({asin}): {e}")
        return {}


# ── Amazon 전체 이미지 크롤링 (fallback) ─────────────

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Amazon colorImages JSON 내 hiRes/large URL 추출 패턴
_PAT_HIRES = re.compile(r'"hiRes"\s*:\s*"(https://m\.media-amazon\.com/images/I/[^"]+)"')
_PAT_LARGE = re.compile(r'"large"\s*:\s*"(https://m\.media-amazon\.com/images/I/[^"]+)"')
# 이미지 ID 추출 (중복 제거용) — e.g. "61aBcDeFgH" from ".../I/61aBcDeFgH._AC_SL1500_.jpg"
_PAT_IMG_ID = re.compile(r'/I/([A-Za-z0-9+_%-]+)\.')


def fetch_amazon_images(asin: str, max_images: int = 15) -> list[str]:
    """Amazon 이미지 URL 수집. SP-API 우선, 실패 시 크롤링 fallback.

    Returns: 이미지 URL 리스트 (최대 max_images개, 중복 제거).
    """
    # 1차: SP-API (안정적)
    sp_urls = fetch_amazon_images_sp_api(asin, max_images)
    if sp_urls:
        return sp_urls
    logger.info(f"SP-API fallback → 크롤링: {asin}")

    # 2차: 크롤링 (fallback)
    url = f"https://www.amazon.com/dp/{asin}"
    try:
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=20)
        if resp.status_code != 200:
            logger.warning(f"Amazon 페이지 접근 실패 ({resp.status_code}): {asin}")
            return []
    except Exception as e:
        logger.warning(f"Amazon 페이지 요청 오류 ({asin}): {e}")
        return []

    html = resp.text

    # hiRes 이미지 전체 수집
    hires_urls = _PAT_HIRES.findall(html)
    large_urls = _PAT_LARGE.findall(html)

    # 이미지 ID 기준 중복 제거 + 순서 유지
    seen_ids: set[str] = set()
    result: list[str] = []

    for img_url in hires_urls:
        m = _PAT_IMG_ID.search(img_url)
        img_id = m.group(1) if m else img_url
        if img_id not in seen_ids:
            seen_ids.add(img_id)
            result.append(img_url)

    # hiRes에 없는 이미지가 large에 있을 수 있음 — 보충
    for img_url in large_urls:
        m = _PAT_IMG_ID.search(img_url)
        img_id = m.group(1) if m else img_url
        if img_id not in seen_ids:
            seen_ids.add(img_id)
            result.append(img_url)

    result = result[:max_images]
    logger.info(f"🔍 Amazon {asin}: {len(result)}장 이미지 발견 (hiRes {len(hires_urls)}, large {len(large_urls)})")
    return result


# ── 다운로드 ─────────────────────────────────────

async def download_product_images(product_id: int, images_json: str) -> dict:
    """Amazon 이미지 다운로드 → 로컬 저장 → image_cache 기록.

    Returns: {product_id, downloaded, failed, local_urls, main_image_url}
    """
    try:
        image_urls = json.loads(images_json) if images_json else []
    except (json.JSONDecodeError, TypeError):
        image_urls = []

    if not image_urls:
        return {
            "product_id": product_id, "downloaded": 0,
            "failed": 0, "local_urls": [], "main_image_url": "",
        }

    # 기존 캐시 확인 — 캐시 수 ≥ 요청 수이면 재사용, 부족하면 전체 재다운로드
    with get_db() as conn:
        existing = conn.execute(
            "SELECT local_path, public_url FROM image_cache WHERE product_id=? ORDER BY image_idx",
            (product_id,),
        ).fetchall()

    if existing:
        valid = [r for r in existing if Path(r["local_path"]).exists()]
        if valid and len(valid) >= len(image_urls):
            urls = [r["public_url"] for r in valid]
            return {
                "product_id": product_id, "downloaded": len(urls),
                "failed": 0, "local_urls": urls,
                "main_image_url": urls[0], "cached": True,
            }
        # 캐시 부족 → 기존 캐시 삭제 후 전체 재다운로드
        if len(valid) < len(image_urls):
            logger.info(f"📸 product {product_id}: 캐시 {len(valid)}장 < 요청 {len(image_urls)}장 → 재다운로드")
            for r in existing:
                try:
                    Path(r["local_path"]).unlink(missing_ok=True)
                except Exception:
                    pass
            with get_db() as conn2:
                conn2.execute("DELETE FROM image_cache WHERE product_id=?", (product_id,))

    product_dir = IMAGES_DIR / str(product_id)
    product_dir.mkdir(parents=True, exist_ok=True)

    retention = _get_retention_days()
    delete_at = (datetime.now(timezone.utc) + timedelta(days=retention)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    downloaded = 0
    failed = 0
    local_urls = []

    for idx, url in enumerate(image_urls):
        if not url or not isinstance(url, str) or not url.startswith("http"):
            failed += 1
            continue
        try:
            # 2026-05-19: sync requests.get 을 to_thread 로 감싸 event loop blocking 회피
            # (process_product_html_only 가 async, concurrency 8 일 때 사실상 직렬화 되던 부분)
            resp = await asyncio.to_thread(requests.get, url, timeout=20, headers=_HEADERS)
            if resp.status_code != 200:
                logger.warning(f"이미지 다운로드 실패 ({resp.status_code}): {url[:80]}")
                failed += 1
                continue

            filename = f"img_{idx:03d}.jpg"
            file_path = product_dir / filename
            # 쿠팡 규격(양변 ≥500, 1:1) 보정 → 흰배경 1000x1000 + JPEG q=85
            # 단변<500 비율 이미지가 쿠팡 승인반려되던 버그 방지 (2026-06-01)
            try:
                img = _PIL_Image.open(BytesIO(resp.content))
                img = _normalize_for_coupang(img)
                img.save(file_path, "JPEG", quality=85, optimize=True)
                _saved_size = file_path.stat().st_size
            except Exception as _e:
                logger.warning(f"이미지 리사이즈 실패 ({idx}), 원본 저장: {_e}")
                file_path.write_bytes(resp.content)
                _saved_size = len(resp.content)

            public_url = f"/api/pa/images/products/{product_id}/{filename}"

            with get_db() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO image_cache
                       (product_id, local_path, public_url, original_url,
                        image_idx, size_bytes, scheduled_delete_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (product_id, str(file_path), public_url, url,
                     idx, _saved_size, delete_at),
                )

            local_urls.append(public_url)
            downloaded += 1
            # 2026-05-19: 매 이미지 INFO 로그는 batch 시 폭발 — debug 로 강등
            logger.debug(f"📸 이미지 저장: {public_url} ({_saved_size:,} bytes, 원본 {len(resp.content):,})")

        except Exception as e:
            logger.warning(f"이미지 다운로드 오류 ({idx}): {e}")
            failed += 1

    return {
        "product_id": product_id,
        "downloaded": downloaded,
        "failed": failed,
        "local_urls": local_urls,
        "main_image_url": local_urls[0] if local_urls else "",
    }


# ── 삭제 예약 (전 채널 등록 완료 시) ────────────────

def mark_images_for_deletion(product_id: int):
    """모든 대상 채널 업로드 완료 시에만 삭제 예약.

    정책 (2026-04-20 변경):
      - 하나의 채널이 성공했더라도 다른 채널이 pending이면 삭제 보류.
      - 이미지가 먼저 삭제되면 나머지 채널이 '이미지 파일 없음'으로 excluded됨.
      - 모든 listings_pa row가 terminal 상태일 때만 삭제 예약.

    2026-08-11 수정: terminal 목록에 removed/archived/rotated/paused 가 빠져 있어,
    채널에서 내려간 상품이 영원히 '진행 중'으로 잡혀 삭제 예약이 안 됐다.
    전 채널 리스팅을 지운 뒤 이 버그가 전면화됐다(모든 상품이 removed).
    실제로 업로드를 더 시도하는 상태는 pending 하나뿐이다.
    """
    with get_db() as conn:
        still_pending = conn.execute(
            """SELECT COUNT(*) AS c FROM listings_pa
               WHERE product_id=? AND status NOT IN
                     ('listed', 'excluded', 'removed', 'archived', 'rotated', 'paused')""",
            (product_id,),
        ).fetchone()
        if still_pending and still_pending["c"] > 0:
            logger.info(f"⏸️ 이미지 삭제 보류: product {product_id} — pending 채널 {still_pending['c']}개 남음")
            return
        conn.execute(
            "UPDATE image_cache SET scheduled_delete_at=? WHERE product_id=?",
            (_now_iso(), product_id),
        )
    logger.info(f"🗑️ 이미지 삭제 예약: product {product_id} (전 채널 완료)")


# ── 만료 이미지 정리 ─────────────────────────────

def cleanup_expired_images() -> dict:
    """scheduled_delete_at이 지난 이미지 파일 삭제 + DB 레코드 정리."""
    now = _now_iso()

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, product_id, local_path FROM image_cache WHERE scheduled_delete_at <= ?",
            (now,),
        ).fetchall()

    if not rows:
        return {"deleted": 0, "errors": 0}

    deleted = 0
    errors = 0
    ids_to_delete = []

    for row in rows:
        try:
            path = Path(row["local_path"])
            if path.exists():
                path.unlink()
                parent = path.parent
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
            ids_to_delete.append(row["id"])
            deleted += 1
        except Exception as e:
            logger.warning(f"이미지 삭제 실패 (id={row['id']}): {e}")
            errors += 1

    if ids_to_delete:
        placeholders = ",".join("?" * len(ids_to_delete))
        with get_db() as conn:
            conn.execute(
                f"DELETE FROM image_cache WHERE id IN ({placeholders})",
                ids_to_delete,
            )

    logger.info(f"🗑️ 이미지 정리 완료: 삭제 {deleted}, 오류 {errors}")
    return {"deleted": deleted, "errors": errors}


def ensure_local_images(product_id: int) -> int:
    """로컬 이미지가 없으면 원본 URL 로 재다운로드. 반환: 확보된 파일 수.

    2026-08-03: 수정(PUT)→재승인 시 쿠팡이 우리 URL 을 다시 크롤하므로,
      삭제된 이미지를 미리 복구해 두지 않으면 "다운로드 할 수 없는 이미지" 로 반려된다.
    """
    import os as _os
    import json as _json
    import asyncio as _asyncio
    from pathlib import Path as _P
    media = _P(__file__).resolve().parent.parent / "media" / "products" / str(product_id)
    if media.is_dir():
        n = len([f for f in _os.listdir(media) if f.lower().endswith((".jpg", ".jpeg", ".png"))])
        if n:
            return n
    try:
        from backend.purchase.database import get_db as _gdb
        with _gdb() as conn:
            row = conn.execute(
                "SELECT images_json, sp_images_json FROM products WHERE id=?",
                (product_id,)).fetchone()
        src = (row["images_json"] or row["sp_images_json"]) if row else None
        if not src:
            logger.warning(f"[ensure-img] product {product_id} 원본 URL 없음 — 복구 불가")
            return 0
        _asyncio.run(download_product_images(product_id, src))
    except Exception as e:
        logger.warning(f"[ensure-img] product {product_id} 재다운로드 실패: {e}")
        return 0
    if media.is_dir():
        n = len([f for f in _os.listdir(media) if f.lower().endswith((".jpg", ".jpeg", ".png"))])
        logger.info(f"[ensure-img] product {product_id} 재다운로드 {n}개")
        return n
    return 0
