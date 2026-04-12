"""PA 이미지 다운로더 — Amazon 이미지 → EC2 로컬 저장 + 자동 삭제 관리.

삭제 정책:
  - 채널 업로드 완료 시 → 즉시 삭제 예약
  - 미등록 → 30일(settings.image_retention_days) 후 자동 삭제
"""
import json
import logging
import os
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

    # 기존 캐시 확인 — 이미 다운로드된 이미지가 있으면 재사용
    with get_db() as conn:
        existing = conn.execute(
            "SELECT local_path, public_url FROM image_cache WHERE product_id=? ORDER BY image_idx",
            (product_id,),
        ).fetchall()

    if existing:
        # 파일이 실제로 존재하는지 확인
        valid = [r for r in existing if Path(r["local_path"]).exists()]
        if valid:
            urls = [r["public_url"] for r in valid]
            return {
                "product_id": product_id, "downloaded": len(urls),
                "failed": 0, "local_urls": urls,
                "main_image_url": urls[0], "cached": True,
            }

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


# ── 삭제 예약 (채널 등록 완료 시) ────────────────

def mark_images_for_deletion(product_id: int):
    """채널 업로드 완료 → 즉시 삭제 예약."""
    with get_db() as conn:
        conn.execute(
            "UPDATE image_cache SET scheduled_delete_at=? WHERE product_id=?",
            (_now_iso(), product_id),
        )
    logger.info(f"🗑️ 이미지 삭제 예약: product {product_id}")


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
