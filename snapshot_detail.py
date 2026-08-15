# -*- coding: utf-8 -*-
"""snapshot_detail.py — 상세페이지 리팩터링 회귀 하네스 (2026-08-07)

registry 도입 / 카피 축소 작업이 "결과물을 바꾸지 않았는지" 확인하는 차단선.
payload 전체를 덤프하지 않는다 — build_payload 는 AI(_ensure_seo_tags)·에디토리얼
서브프로세스(180초)를 타서 스냅샷 용도로 부적합하다. 대신 이번 작업이 실제로
건드리는 3가지 불변식만 좁게 고정한다:

  A. 배너 URL 상수      — registry 파생으로 바꿔도 URL 이 같아야 함(재등록 회피의 핵심)
  B. 네이버 HTML 조립   — _build_pa_html 이 고정 입력에 대해 같은 문자열을 내야 함
  C. 쿠팡 contents 순서 — 상품별 이미지 블록 URL 배열(순서 포함)

C 는 fast=True + PA_SKIP_GEMINI=1 로 호출해 AI 를 완전히 배제한다(결정적).

사용법:
  PYTHONPATH=. .venv/bin/python snapshot_detail.py capture   # 베이스라인 기록
  PYTHONPATH=. .venv/bin/python snapshot_detail.py verify    # 베이스라인 대비 diff
"""
import hashlib
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("PA_SKIP_GEMINI", "1")   # ★결정적 캡처 — 비전/텍스트 AI 배제

BASE = Path(__file__).resolve().parent
SNAP = BASE / "snapshots" / "detail_baseline.json"
N_SAMPLES = 30


def _db():
    from backend.purchase.database import get_db
    return get_db()


def pick_samples(n=N_SAMPLES):
    """정책 분기를 고루 덮는 표본. listings_pa(인덱스) → products 로 가볍게."""
    from backend.purchase.database import get_db
    media = BASE / "backend/purchase/media/products"
    picked, seen = [], set()

    def _take(rows, tag, cap):
        got = 0
        for r in rows:
            pid = r["product_id"] if "product_id" in r.keys() else r["id"]
            if pid in seen:
                continue
            seen.add(pid)
            picked.append({"product_id": pid, "bucket": tag})
            got += 1
            if got >= cap:
                break

    with get_db() as conn:
        listed = conn.execute(
            "SELECT product_id FROM listings_pa WHERE channel='coupang' AND status='listed' "
            "ORDER BY id DESC LIMIT 400").fetchall()
    ids = [r["product_id"] for r in listed]

    # 버킷: self_made / amazon / 구성품컷 보유 / 인포그래픽 보유 / 스펙표 보유
    buckets = {"self_made": [], "amazon": [], "components": [], "infographic": [], "spec": []}
    with get_db() as conn:
        for pid in ids:
            row = conn.execute("SELECT image_policy FROM products WHERE id=?", (pid,)).fetchone()
            pol = (row["image_policy"] if row else None) or "amazon"
            d = media / str(pid)
            if (d / "components_cut.jpg").is_file():
                buckets["components"].append(pid)
            if (d / "infographic.jpg").is_file():
                buckets["infographic"].append(pid)
            if list(d.glob("spec*.jpg")):
                buckets["spec"].append(pid)
            buckets["self_made" if pol == "self_made" else "amazon"].append(pid)

    per = max(2, n // len(buckets))
    for tag, lst in buckets.items():
        for pid in lst:
            if pid in seen:
                continue
            seen.add(pid)
            picked.append({"product_id": pid, "bucket": tag})
            if sum(1 for p in picked if p["bucket"] == tag) >= per:
                break
    return picked[:n]


def capture_a():
    """A. 배너 URL 상수"""
    from backend.purchase.services.coupang_lister import STATIC_BANNER_PATHS, CUSTOMS_BANNER_PATH
    from backend_shared._config import PUBLIC_BASE_URL
    base = (PUBLIC_BASE_URL or "").rstrip("/")
    return {
        "public_base_url": base,
        "customs": CUSTOMS_BANNER_PATH,
        "static": list(STATIC_BANNER_PATHS),
        "absolute": [f"{base}{CUSTOMS_BANNER_PATH}"] + [f"{base}{r}" for r in STATIC_BANNER_PATHS],
    }


FIXED_IMGS = [
    "/api/pa/images/products/999999/img_000.jpg",
    "/api/pa/images/products/999999/img_001.jpg",
    "/api/pa/images/products/999999/img_002.jpg",
]


def capture_b():
    """B. 네이버 HTML 조립 — 고정 입력 → 고정 출력"""
    from backend.purchase.services.ai_processor import _build_pa_html, PA_SECTIONS
    html = _build_pa_html(FIXED_IMGS)
    return {
        "sections": list(PA_SECTIONS),
        "length": len(html),
        "sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        "head": html[:200],
    }


def capture_c(samples):
    """C. 쿠팡 contents URL 배열 (fast=True, AI 없음)"""
    from backend.purchase.services.coupang_lister import (
        build_detail_contents, _get_product_images)
    out = {}
    for s in samples:
        pid = s["product_id"]
        try:
            imgs = _get_product_images(pid) or []
            blocks = build_detail_contents(pid, imgs, fast=True, shared_editorial=None)
            urls = [b["contentDetails"][0]["content"] for b in blocks]
            out[str(pid)] = {"bucket": s["bucket"], "n": len(urls), "urls": urls}
        except Exception as e:
            out[str(pid)] = {"bucket": s["bucket"], "error": f"{type(e).__name__}: {e}"}
    return out


def do_capture():
    samples = pick_samples()
    print(f"표본 {len(samples)}개 선정")
    snap = {
        "A_banner_urls": capture_a(),
        "B_naver_html": capture_b(),
        "C_coupang_contents": capture_c(samples),
    }
    SNAP.parent.mkdir(parents=True, exist_ok=True)
    SNAP.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for v in snap["C_coupang_contents"].values() if "urls" in v)
    print(f"베이스라인 기록: {SNAP}")
    print(f"  A 배너 {len(snap['A_banner_urls']['absolute'])}개")
    print(f"  B 네이버 HTML {snap['B_naver_html']['length']:,}자 sha={snap['B_naver_html']['sha256'][:12]}")
    print(f"  C 쿠팡 contents {ok}/{len(samples)} 상품 캡처")
    return 0


def do_verify():
    if not SNAP.exists():
        print(f"베이스라인 없음: {SNAP} — 먼저 capture 실행"); return 2
    old = json.loads(SNAP.read_text(encoding="utf-8"))
    samples = [{"product_id": int(k), "bucket": v.get("bucket", "?")}
               for k, v in old["C_coupang_contents"].items()]
    new = {"A_banner_urls": capture_a(), "B_naver_html": capture_b(),
           "C_coupang_contents": capture_c(samples)}

    diffs = []
    if old["A_banner_urls"]["absolute"] != new["A_banner_urls"]["absolute"]:
        diffs.append("A 배너 URL 변경 — ★재등록 유발!")
        for o, n in zip(old["A_banner_urls"]["absolute"], new["A_banner_urls"]["absolute"]):
            if o != n:
                diffs.append(f"    {o}\n  → {n}")
    if old["B_naver_html"]["sha256"] != new["B_naver_html"]["sha256"]:
        diffs.append(f"B 네이버 HTML 변경 "
                     f"({old['B_naver_html']['length']:,} → {new['B_naver_html']['length']:,}자)")
    for pid, ov in old["C_coupang_contents"].items():
        nv = new["C_coupang_contents"].get(pid, {})
        if ov.get("urls") != nv.get("urls"):
            diffs.append(f"C product {pid} ({ov.get('bucket')}) contents 변경: "
                         f"{ov.get('n')} → {nv.get('n')} 블록")

    if not diffs:
        print("✅ 변경 없음 — 결과물 동일 (재등록 불필요)")
        return 0
    print(f"⚠️  차이 {len(diffs)}건")
    for d in diffs:
        print("  " + d)
    return 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "capture"
    sys.exit(do_capture() if cmd == "capture" else do_verify())
