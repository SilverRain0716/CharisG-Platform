"""PA 이미지 다운로더 — Amazon 이미지 → EC2 로컬 저장 + 자동 삭제 관리.

이미지 수집 우선순위:
  1. SP-API Catalog Items (안정적, 공식 API)
  2. Amazon 페이지 크롤링 (fallback)

삭제 정책:
  - 채널 업로드 완료 시 → 즉시 삭제 예약
  - 미등록 → 30일(settings.image_retention_days) 후 자동 삭제
"""
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)

MEDIA_ROOT = Path(os.environ.get(
    "PA_MEDIA_ROOT",
    str(Path(__file__).resolve().parent.parent / "media"),
))
IMAGES_DIR = MEDIA_ROOT / "products"

DEFAULT_RETENTION_DAYS = 30

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
    """SP-API 이미지 URL 수집 — sp_api_facts 단일 호출 + 캐시 경유.

    같은 ASIN 에 대해 7일 내 재호출 0 (DB 캐시).
    """
    try:
        from backend.purchase.services.sp_api_facts import get_image_urls
    except ImportError:
        logger.warning("sp_api_facts 모듈 없음 — SP-API 이미지 수집 불가")
        return []
    return get_image_urls(asin, max_images)


def fetch_product_info_sp_api(asin: str) -> dict:
    """SP-API 상품 기본정보 — sp_api_facts 경유 (캐시 우선).

    Returns (기존 형식 유지): {title, brand, description, bullet_points, images}.
    """
    try:
        from backend.purchase.services.sp_api_facts import get_facts_for_promote
    except ImportError:
        return {}
    try:
        return get_facts_for_promote(asin)
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
            resp = requests.get(url, timeout=20, headers=_HEADERS)
            if resp.status_code != 200:
                logger.warning(f"이미지 다운로드 실패 ({resp.status_code}): {url[:80]}")
                failed += 1
                continue

            filename = f"img_{idx:03d}.jpg"
            file_path = product_dir / filename
            file_path.write_bytes(resp.content)

            public_url = f"/api/pa/images/products/{product_id}/{filename}"

            with get_db() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO image_cache
                       (product_id, local_path, public_url, original_url,
                        image_idx, size_bytes, scheduled_delete_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (product_id, str(file_path), public_url, url,
                     idx, len(resp.content), delete_at),
                )

            local_urls.append(public_url)
            downloaded += 1
            logger.info(f"📸 이미지 저장: {public_url} ({len(resp.content):,} bytes)")

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
      - 모든 listings_pa row가 terminal 상태(listed/excluded)일 때만 삭제 예약.
    """
    with get_db() as conn:
        still_pending = conn.execute(
            """SELECT COUNT(*) AS c FROM listings_pa
               WHERE product_id=? AND status NOT IN ('listed', 'excluded')""",
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
