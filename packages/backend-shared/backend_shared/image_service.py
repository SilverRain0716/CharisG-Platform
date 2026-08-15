"""
image_service.py — 이미지 다운로드 + 리사이즈
마켓별 규격에 맞춰 이미지 가공

저장 경로: PROJECT_ROOT/media/products/{product_id}/
"""
import logging
import os
from pathlib import Path
from typing import Optional

import requests

from backend_shared.context import get_db

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
MEDIA_DIR = PROJECT_ROOT / "media" / "products"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# 마켓별 이미지 규격
PLATFORM_SPECS = {
    "smartstore": {"max_width": 860, "max_height": 860, "format": "JPEG", "quality": 90},
    "coupang":    {"max_width": 500, "max_height": 500, "format": "JPEG", "quality": 85},
    "amazon":     {"max_width": 1600, "max_height": 1600, "format": "JPEG", "quality": 90},
    "ebay":       {"max_width": 1600, "max_height": 1600, "format": "JPEG", "quality": 85},
}


async def download_and_resize_images(
    product_id: int,
    image_urls: list[str],
    platform: str = "smartstore",
) -> dict:
    """
    이미지 다운로드 + 마켓 규격 리사이즈

    Returns: {
        "product_id": 1,
        "total": 5,
        "processed": 3,
        "failed": 2,
        "processed_paths": ["/media/products/1/img_001.jpg", ...],
    }
    """
    spec = PLATFORM_SPECS.get(platform, PLATFORM_SPECS["smartstore"])
    product_dir = MEDIA_DIR / str(product_id)
    product_dir.mkdir(parents=True, exist_ok=True)

    processed_paths = []
    failed = 0

    for idx, url in enumerate(image_urls):
        if not url or not url.startswith("http"):
            failed += 1
            continue

        try:
            # 다운로드
            resp = requests.get(url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/123.0.0.0",
            })
            if resp.status_code != 200:
                logger.warning(f"⚠️ 이미지 다운로드 실패 ({resp.status_code}): {url[:80]}")
                failed += 1
                continue

            content_type = resp.headers.get("content-type", "")
            if "image" not in content_type and not url.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                logger.warning(f"⚠️ 이미지가 아닌 콘텐츠: {content_type}")
                failed += 1
                continue

            # 리사이즈
            resized_path = _resize_image(
                image_bytes=resp.content,
                output_path=product_dir / f"img_{idx:03d}.jpg",
                max_width=spec["max_width"],
                max_height=spec["max_height"],
                quality=spec["quality"],
                output_format=spec["format"],
            )

            if resized_path:
                rel_path = str(resized_path.relative_to(PROJECT_ROOT))
                processed_paths.append(rel_path)

                # product_images 테이블에 기록
                _save_image_record(product_id, url, rel_path, "resize")
            else:
                failed += 1

        except Exception as e:
            logger.warning(f"⚠️ 이미지 처리 오류 ({idx}): {e}")
            failed += 1

    return {
        "product_id": product_id,
        "platform": platform,
        "spec": spec,
        "total": len(image_urls),
        "processed": len(processed_paths),
        "failed": failed,
        "processed_paths": processed_paths,
    }


def _resize_image(
    image_bytes: bytes,
    output_path: Path,
    max_width: int,
    max_height: int,
    quality: int = 90,
    output_format: str = "JPEG",
) -> Optional[Path]:
    """Pillow로 이미지 리사이즈"""
    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(image_bytes))

        # RGBA → RGB 변환 (JPEG 저장용)
        if img.mode in ("RGBA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # 리사이즈 (비율 유지)
        w, h = img.size
        if w > max_width or h > max_height:
            ratio = min(max_width / w, max_height / h)
            new_size = (int(w * ratio), int(h * ratio))
            img = img.resize(new_size, Image.LANCZOS)

        img.save(output_path, format=output_format, quality=quality, optimize=True)
        logger.info(f"📸 리사이즈: {w}×{h} → {img.size[0]}×{img.size[1]} ({output_path.name})")
        return output_path

    except ImportError:
        logger.error("Pillow 미설치 — pip install Pillow")
        # Pillow 없으면 원본 그대로 저장
        output_path.write_bytes(image_bytes)
        return output_path
    except Exception as e:
        logger.error(f"이미지 리사이즈 실패: {e}")
        return None


def _save_image_record(product_id: int, original_url: str, processed_path: str, processing_type: str):
    """product_images 테이블에 기록"""
    try:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO product_images
                   (product_id, original_url, processed_path, processing_type, status)
                   VALUES (?, ?, ?, ?, 'completed')""",
                (product_id, original_url, processed_path, processing_type),
            )
    except Exception as e:
        logger.warning(f"이미지 레코드 저장 실패: {e}")
