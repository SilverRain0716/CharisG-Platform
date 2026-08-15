# -*- coding: utf-8 -*-
import os
"""B 재설계 상세 — 공유 에디토리얼(부모 1회) + 옵션별 누끼/실제컷 조립.
generate_shared_editorial(master_pid): vision 1세트 → 사진-free 섹션(media/.../edsec_*.jpg) → URL 리스트(캐시).
build_b_contents(option_pid, shared_urls): 쿠팡 contents 배열
  = [브랜드배너] + [누끼 대표컷] + [공유 에디토리얼] + [옵션 실제컷≤2] + [스펙표] + [정보배너].
"""
import subprocess, sys, os, json
from pathlib import Path

BASE = Path.home() / "CharisG-Platform/charisg-platform"
RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render_editorial_runner.py")


def generate_shared_editorial(master_pid, force=False):
    media = BASE / "backend/purchase/media/products" / str(master_pid)
    man = media / "ed_manifest.json"
    if not force and man.exists():
        try:
            u = json.loads(man.read_text())
            if u:
                return u
        except Exception:
            pass
    try:
        r = subprocess.run([sys.executable, RUNNER, str(master_pid)], cwd=str(BASE),
                           env={**os.environ, "PYTHONPATH": str(BASE)}, timeout=600, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[edit] master {master_pid} rc={r.returncode} {r.stderr[-200:]}")
    except Exception as e:
        print(f"[edit] master {master_pid} 렌더 예외: {e}")
        return []
    if man.exists():
        try:
            return json.loads(man.read_text())
        except Exception:
            return []
    return []


def _block(u):
    return {"contentsType": "IMAGE_NO_SPACE", "contentDetails": [{"content": u, "detailType": "IMAGE", "altText": ""}]}


def build_b_contents(option_pid, shared_urls):
    """옵션(자식) 상세 contents — 누끼 히어로 + 공유 에디토리얼 + 옵션 실제컷."""
    from backend.purchase.services.coupang_lister import (
        PUBLIC_BASE_URL, STATIC_BANNER_PATHS, select_representative_image, _get_product_images,
    )
    base = PUBLIC_BASE_URL.rstrip("/")

    def _abs(u):
        return u if str(u).startswith("http") else f"{base}{u}"

    cp = []
    # ① 브랜드 정품 배너
    cp.append(_block(_abs(STATIC_BANNER_PATHS[0])))
    # ② AI 에디토리얼(대표 1회 생성, 전 옵션 공유) — ★누끼 임베드 흰카드 + 텍스트 카드.
    #   아마존 원본 사진(실제컷/라이프스타일) 미사용(저작권). 제품 이미지는 여기 누끼 1장뿐.
    if shared_urls:
        for u in shared_urls:
            cp.append(_block(_abs(u)))
    else:
        # 폴백: 에디토리얼 생성 실패 시 누끼 1장이라도(rep_nuki 우선)
        rep_path = BASE / "backend/purchase/media/products" / str(option_pid) / "rep_nuki.jpg"
        if rep_path.exists():
            cp.append(_block(_abs(f"/api/pa/images/products/{option_pid}/rep_nuki.jpg")))
        else:
            try:
                r = select_representative_image(int(option_pid))
                if r:
                    cp.append(_block(_abs(r)))
            except Exception:
                pass
    # ⑤ 세부사항 스펙표
    try:
        from backend.purchase.services.spec_table import render_spec_table
        spec = render_spec_table(int(option_pid))
        if spec:
            cp.append(_block(_abs(spec)))
    except Exception:
        pass
    # ⑥ 정보 배너(배송/아마존/필독)
    for rel in STATIC_BANNER_PATHS[1:]:
        cp.append(_block(_abs(rel)))
    return cp
