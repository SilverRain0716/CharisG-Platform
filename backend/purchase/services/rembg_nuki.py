# -*- coding: utf-8 -*-
"""rembg 배경제거 누끼 — 깨끗한 흰배경 제품컷이 없을 때(라이프스타일뿐) 제품만 오려
흰배경에 합성해 rembg_nuki.jpg 생성 (Gemini 불필요, 대법원 98다43366: 제품=저작권 무보호).

★검증 게이트(흰테두리≥0.55): 모델/동물이 남은 결과(Tervis처럼 손·텍스트 제거 성공 vs
개 세트처럼 동물 잔존)를 자동 구분 — 잔존 시 흰테두리 낮아 탈락.
★메모리 격리: onnx(피크~530MB)는 subprocess(python -m ...rembg_nuki <pid>)로 실행해
pa-api 상주 프로세스에 로드하지 않음. 결과 파일만 select_representative/all_nuki 가 소비.
"""
import os
import sys
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_MEDIA = Path(__file__).resolve().parent.parent / "media"
_WHITE_MIN = 0.55       # 합성결과 흰테두리 최소(모델 잔존 배제)
_MAX_TRY = 5            # 소스 이미지 최대 시도 수


def _media_dir(pid):
    return _MEDIA / "products" / str(pid)


def _border_white(im, strip=0.10):
    im = im.convert("RGB")
    w, h = im.size
    px = im.load()
    sw, sh = max(1, int(w * strip)), max(1, int(h * strip))
    pts = white = 0
    for x in range(0, w, 4):
        for y in list(range(0, sh)) + list(range(h - sh, h)):
            r, g, b = px[x, y]
            pts += 1
            if r >= 238 and g >= 238 and b >= 238:
                white += 1
    for y in range(0, h, 4):
        for x in list(range(0, sw)) + list(range(w - sw, w)):
            r, g, b = px[x, y]
            pts += 1
            if r >= 238 and g >= 238 and b >= 238:
                white += 1
    return white / max(1, pts)


def _sources(pid):
    """marketing(텍스트그래픽) 제외한 이미지 local_path (image_idx 순). 라이프스타일/제품 포함."""
    from backend.purchase.database import get_db
    try:
        from backend.purchase.services.image_classifier import classify_images
        cls = classify_images(pid)
    except Exception:
        cls = {}
    with get_db() as conn:
        rows = conn.execute(
            "SELECT local_path FROM image_cache WHERE product_id=? ORDER BY image_idx",
            (pid,),
        ).fetchall()
    out = []
    for r in rows:
        lp = r["local_path"]
        if lp and os.path.isfile(lp) and cls.get(lp, "photo") != "marketing":
            out.append(lp)
    return out


def generate(pid, force=False):
    """rembg_nuki.jpg 생성. 성공 시 파일경로, 실패 시 None. (인프로세스 — onnx 로드됨)"""
    from PIL import Image
    md = _media_dir(pid)
    out = md / "rembg_nuki.jpg"
    if out.is_file() and not force:
        return str(out)
    srcs = _sources(pid)
    if not srcs:
        return None
    try:
        from rembg import remove, new_session
    except Exception as e:
        logger.warning(f"[rembg] import 실패: {e}")
        return None
    sess = new_session("u2netp")   # 경량모델(t3.micro)
    for src in srcs[:_MAX_TRY]:
        try:
            im = Image.open(src).convert("RGBA")
            if min(im.size) < 400:
                continue
            cut = remove(im, session=sess, alpha_matting=True,
                         alpha_matting_foreground_threshold=270,
                         alpha_matting_background_threshold=20,
                         alpha_matting_erode_size=11)  # 경계 정밀화(투명/미세 엣지)
            bb = cut.getbbox()
            if not bb:
                continue
            c = cut.crop(bb)
            canvas = Image.new("RGBA", c.size, (255, 255, 255, 255))
            canvas.alpha_composite(c)
            fin = canvas.convert("RGB")
            fin.thumbnail((1000, 1000))
            final = Image.new("RGB", (1000, 1000), (255, 255, 255))
            final.paste(fin, ((1000 - fin.width) // 2, (1000 - fin.height) // 2))
            if _border_white(final) >= _WHITE_MIN:
                md.mkdir(parents=True, exist_ok=True)
                final.save(out, "JPEG", quality=90, optimize=True)
                logger.info(f"[rembg] product {pid} rembg_nuki 생성 ({os.path.basename(src)})")
                return str(out)
        except Exception as e:
            logger.warning(f"[rembg] {pid} {os.path.basename(src)} 실패: {e}")
            continue
    return None


def gen_rembg_nuki_subprocess(pid, timeout=180):
    """메모리 격리 — subprocess 로 generate 실행. rembg_nuki.jpg 있으면 경로, 없으면 None."""
    import subprocess
    md = _media_dir(pid)
    out = md / "rembg_nuki.jpg"
    if out.is_file():
        return str(out)
    repo = Path(__file__).resolve().parents[3]
    try:
        subprocess.run(
            [sys.executable, "-m", "backend.purchase.services.rembg_nuki", str(pid)],
            cwd=str(repo), timeout=timeout,
            env={**os.environ, "PYTHONPATH": str(repo)},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logger.warning(f"[rembg] subprocess 실패 {pid}: {e}")
    return str(out) if out.is_file() else None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _pid = int(sys.argv[1])
    _r = generate(_pid, force=("--force" in sys.argv))
    print(_r or "FAIL")
