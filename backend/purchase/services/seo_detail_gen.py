# -*- coding: utf-8 -*-
"""리치 상세 자동생성 — render_detail_runner.py(비전+Playwright) 실행 후 media/products/{pid}/seo_*.jpg
   + seo_detail.json 매니페스트 작성. build_detail_contents 가 매니페스트 우선 사용(리치).
   삭제됐던 /tmp 래퍼를 프로덕션 이식. 2026-06-21."""
import json, subprocess, sys, os, logging
from pathlib import Path
from PIL import Image

logger = logging.getLogger(__name__)
_BASE = Path.home() / "CharisG-Platform/charisg-platform"
_RENDER = _BASE / "backend/purchase/services/render_detail_runner.py"
_OUT = Path("/tmp/render_detail")
_W = 1080


def generate_seo_detail(product_id, force: bool = False) -> bool:
    """product_id 의 리치 상세 생성 → media/products/{pid}/seo_detail.json. 이미 있으면 스킵(force=False)."""
    pid = str(product_id)
    media = _BASE / "backend/purchase/media/products" / pid
    if not force and (media / "seo_detail.json").exists():
        return True
    # 1) 렌더(비전+Playwright) — 서브프로세스(전역/Playwright 격리)
    try:
        r = subprocess.run([sys.executable, str(_RENDER), pid], cwd=str(_BASE),
                           env={**os.environ, "PYTHONPATH": str(_BASE)},
                           timeout=600, capture_output=True, text=True)
    except Exception as e:
        logger.warning(f"[seo_detail] {pid} 렌더 실패: {e}")
        return False
    src = _OUT / pid
    if not src.exists():
        logger.warning(f"[seo_detail] {pid} 렌더산출 없음 (rc={r.returncode})")
        return False
    # 2) media 로 복사 + manifest (삭제됐던 seo_wrap 로직)
    media.mkdir(parents=True, exist_ok=True)
    manifest = []
    rep = src / "_photo.jpg"
    if rep.exists():
        im = Image.open(rep).convert("RGB")
        if im.width != _W: im = im.resize((_W, int(im.height * _W / im.width)))
        im.save(media / "seo_rep.jpg", "JPEG", quality=88)
        manifest.append(f"/api/pa/images/products/{pid}/seo_rep.jpg")
    secs = sorted(src.glob("[0-9]*_*.png"), key=lambda p: int(p.name.split("_")[0]))
    for i, sec in enumerate(secs):
        im = Image.open(sec).convert("RGB")
        if im.width != _W: im = im.resize((_W, int(im.height * _W / im.width)))
        if im.height > 5000: im = im.resize((_W, 5000))
        im.save(media / f"seo_sec{i}.jpg", "JPEG", quality=85)
        manifest.append(f"/api/pa/images/products/{pid}/seo_sec{i}.jpg")
    if not manifest:
        return False
    (media / "seo_detail.json").write_text(json.dumps(manifest, ensure_ascii=False))
    logger.info(f"[seo_detail] {pid} 리치 상세 생성 → {len(manifest)} blocks")
    return True
