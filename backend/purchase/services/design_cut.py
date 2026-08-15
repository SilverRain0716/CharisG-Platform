# -*- coding: utf-8 -*-
"""0-clean 상품(깨끗한 제품사진 0장, 전부 marketing/lifestyle) 대표용 AI 디자인컷 생성.
   소스 이미지(제품이 보이는 것)를 Nano Banana(flash-image)로 편집 → 텍스트·모델·로고 없는
   깨끗한 스튜디오 제품컷을 design_cut.jpg 로 저장. select_representative_image 가 자동 대표 채택.
   실패 시 None(기존처럼 rep_nuki 폴백 — 무회귀)."""
import os
import time
import base64
import logging
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image as PI

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)
_MEDIA = Path(__file__).resolve().parent.parent / "media"
_GEN_MODEL = "gemini-2.5-flash-image"
_PROMPT = (
    "Premium e-commerce product photographer. Use the provided image ONLY as a reference for the "
    "product's true appearance (shape, color, material must stay faithful and unchanged). Generate a "
    "NEW clean studio catalog photo of the SINGLE main product by itself, centered and fully visible. "
    "ABSOLUTELY NO text, letters, numbers, logos, brand marks, watermarks, callouts, promotional "
    "graphics, icons or diagrams anywhere in the image or on the product. NO people, NO hands, NO "
    "models, NO lifestyle scene. STRICTLY EXCLUDE any packaging text or overlays. Pure white background, "
    "professional soft studio lighting, subtle natural shadow, generous margin, high-end e-commerce look."
)


def _keys():
    ks = []
    for n in ("GEMINI_API_KEY_5", "GEMINI_API_KEY_FALLBACK", "GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"):
        v = os.environ.get(n)
        if v and v not in ks:
            ks.append(v)
    return ks


def _source_image(pid):
    """디자인컷 참조 소스 — 제품이 가장 잘 보이는 이미지. 선호: photo > marketing(제품+텍스트) > lifestyle(모델/야외)."""
    with get_db() as c:
        rows = c.execute(
            "SELECT local_path FROM image_cache WHERE product_id=? AND public_url IS NOT NULL ORDER BY image_idx",
            (pid,),
        ).fetchall()
    paths = [r["local_path"] for r in rows if r["local_path"] and os.path.isfile(r["local_path"])]
    if not paths:
        return None

    def _big(p):
        try:
            return min(PI.open(p).size) >= 400
        except Exception:
            return True

    big = [p for p in paths if _big(p)] or paths
    try:
        from backend.purchase.services.image_classifier import classify_images
        cls = classify_images(pid)
        for want in ("photo", "marketing", "lifestyle"):
            for p in big:
                if cls.get(p, "photo") == want:
                    return p
    except Exception:
        pass
    return big[0]


def is_zero_clean(pid):
    """깨끗한 제품사진(photo)이 0장이면 True. ★_get_product_images/clean_local_paths는 0-clean에도
    '대표 1장 폴백'을 반환해 부정확 → 실제 photo 분류 개수로 판정."""
    from backend.purchase.services.image_classifier import image_paths, classify_images
    paths = image_paths(pid)
    if not paths:
        return False   # 이미지 자체가 없으면 디자인컷 소스도 없음
    try:
        cls = classify_images(pid)
    except Exception:
        return False
    return sum(1 for p in paths if cls.get(p, "photo") == "photo") == 0


def gen_design_cut(pid, force=False):
    """0-clean 상품에 design_cut.jpg 생성. 성공 시 파일경로, 실패 시 None."""
    md = _MEDIA / "products" / str(pid)
    out = md / "design_cut.jpg"
    if out.is_file() and not force:
        return str(out)
    ref = _source_image(pid)
    if not ref:
        return None
    try:
        b = base64.b64encode(open(ref, "rb").read()).decode()
    except Exception:
        return None
    body = {"contents": [{"parts": [{"text": _PROMPT}, {"inline_data": {"mime_type": "image/jpeg", "data": b}}]}],
            "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{_GEN_MODEL}:generateContent?key="
    md.mkdir(parents=True, exist_ok=True)
    for rnd in range(2):
        for k in _keys():
            try:
                r = requests.post(url + k, json=body, timeout=180)
                if r.status_code == 200:
                    for p in r.json()["candidates"][0]["content"]["parts"]:
                        inl = p.get("inline_data") or p.get("inlineData")
                        if inl and inl.get("data"):
                            im = PI.open(BytesIO(base64.b64decode(inl["data"]))).convert("RGB")
                            if im.size != (1000, 1000):
                                im.thumbnail((1000, 1000))
                                cv = PI.new("RGB", (1000, 1000), (255, 255, 255))
                                cv.paste(im, ((1000 - im.width) // 2, (1000 - im.height) // 2))
                                im = cv
                            im.save(out, "JPEG", quality=90, optimize=True)
                            logger.info(f"[design-cut] product {pid} 생성 완료 (ref={os.path.basename(ref)})")
                            return str(out)
                else:
                    logger.info(f"[design-cut] {pid} status {r.status_code} → 키전환")
            except Exception as e:
                logger.info(f"[design-cut] {pid} 예외 {str(e)[:50]}")
            time.sleep(1.2)
    logger.warning(f"[design-cut] product {pid} 생성 실패 — rep_nuki 폴백")
    return None
