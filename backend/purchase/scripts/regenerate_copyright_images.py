"""쿠팡 저작권 침해 통지 대응 — 3 products 이미지 자체 재생성.

대상 (2026-05-21):
  - productId 9525055942  / sellerProductId 16182985879  K-SECRET 레티날 세럼
  - productId 9510438244  / sellerProductId 16182988225  K-SECRET 레티날 아이크림
  - productId 95380350784 / sellerProductId 16190598261  아코스 어린이용 현미경

흐름:
  1. get_seller_product → items[].images cdnPath 추출
  2. 이미지 download → {DL_DIR}/{productId}/original/
  3. Gemini Nano Banana (image-preview) generateContent — 참조 + prompt → 새 이미지
  4. {DL_DIR}/{productId}/new/ 에 저장
  5. info.json 으로 매핑 기록

쿠팡 PUT (update) + 이메일 첨부는 본 스크립트 출력 검토 후 별도 단계.
"""
import os
import sys
import json
import time
import base64
import hmac
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import requests

# Load .env (local dev)
env_path = Path(__file__).resolve().parents[3] / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

COUPANG_ACCESS_KEY = os.environ["COUPANG_ACCESS_KEY"]
COUPANG_SECRET_KEY = os.environ["COUPANG_SECRET_KEY"]
COUPANG_VENDOR_ID = os.environ["COUPANG_VENDOR_ID"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# 출력 폴더 — EC2 임시 폴더 (실행 후 scp 로 local 다운로드 폴더로 가져옴)
DL_DIR = Path(os.environ.get("COPYRIGHT_OUT_DIR", "/tmp/copyright_reply"))

# 대상 product 목록
TARGETS = [
    # (productId, sellerProductId, asin, title_ko, prompt_kind)
    ("9525055942",  "16182985879", "B0CP3C7TTJ",
     "K-SECRET SEOUL 1988 레티날 세럼", "cosmetics_serum"),
    ("9510438244",  "16182988225", "B0DGGBYDRZ",
     "K-SECRET SEOUL 1988 레티날 아이크림", "cosmetics_eyecream"),
    ("95380350784", "16190598261", "B0F8VJDPJF",
     "아코스 어린이용 현미경 1000배 휴대용 디지털 미니스코프", "kids_microscope"),
]

# Prompt 템플릿 — non-infringing, 일반 lifestyle/abstract 구도 강조
PROMPT_BASE = (
    "Create a NEW product image for a Korean e-commerce listing. "
    "IMPORTANT: Do NOT copy or replicate the visual style, packaging design, "
    "logos, fonts, layout, or specific composition of the reference image. "
    "Create an ORIGINAL, generic representation of this product category "
    "with completely different visual style, background, and composition. "
    "Output: square 1024x1024, clean white or light gradient background, "
    "professional product photography style. NO text, NO logos, NO brand names."
)

PROMPT_KIND_HINTS = {
    "cosmetics_serum": "A generic Korean skincare serum bottle (different shape from reference) on clean background.",
    "cosmetics_eyecream": "A generic Korean skincare eye cream jar/tube (different shape from reference) on clean background.",
    "kids_microscope": "A generic handheld digital microscope toy for children on clean white background.",
}

# Gemini 모델 — image preview (Nano Banana). 2.5 미사용 시 3.1 시도
GEMINI_MODELS = [
    "gemini-2.5-flash-image-preview",
    "gemini-3.1-flash-image-preview",
]
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"


def _signature(method: str, path: str, query: str = "") -> dict:
    ts = datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")
    message = ts + method + path + query
    sig = hmac.new(COUPANG_SECRET_KEY.encode(), message.encode(), hashlib.sha256).hexdigest()
    return {
        "Authorization": (
            f"CEA algorithm=HmacSHA256, access-key={COUPANG_ACCESS_KEY}, "
            f"signed-date={ts}, signature={sig}"
        ),
        "Content-Type": "application/json",
    }


def get_seller_product(seller_product_id: str) -> dict | None:
    """쿠팡 sellerProduct 상세 조회."""
    path = f"/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{seller_product_id}"
    headers = _signature("GET", path)
    url = f"https://api-gateway.coupang.com{path}"
    r = requests.get(url, headers=headers, timeout=20)
    if r.status_code != 200:
        print(f"  쿠팡 조회 실패 ({r.status_code}): {r.text[:200]}")
        return None
    return r.json()


def extract_image_urls(seller_product: dict) -> list[tuple[str, str]]:
    """[(imageType, url), ...] 추출. cdnPath 는 path-only 라 쿠팡 CDN 도메인 prefix 부여."""
    out = []
    data = seller_product.get("data") if isinstance(seller_product, dict) else None
    if not isinstance(data, dict):
        return out
    for item in data.get("items") or []:
        for img in item.get("images") or []:
            path = (img.get("cdnPath") or "").strip()
            if not path:
                continue
            if path.startswith("http"):
                url = path
            else:
                # 쿠팡 CDN — image*.coupangcdn.com 도메인 prefix
                url = f"https://image10.coupangcdn.com/image/{path.lstrip('/')}"
                # extension 보강 (cdnPath 가 확장자 없는 경우 다수)
                if not url.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    url = url + ".jpg"
            out.append((img.get("imageType", "DETAIL"), url))
    # 중복 url 제거
    seen = set()
    uniq = []
    for t, u in out:
        if u in seen:
            continue
        seen.add(u)
        uniq.append((t, u))
    return uniq


def download(url: str, dst: Path) -> bool:
    try:
        r = requests.get(url, timeout=20)
        if r.status_code == 200:
            dst.write_bytes(r.content)
            return True
        print(f"    download fail {r.status_code}: {url[:80]}")
    except Exception as e:
        print(f"    download exc: {type(e).__name__}: {e}")
    return False


def gemini_generate_image(prompt: str, ref_image_bytes: bytes, ref_mime: str = "image/jpeg") -> bytes | None:
    """Gemini image preview 모델로 새 이미지 생성. 첫 1개 성공 모델 사용."""
    ref_b64 = base64.b64encode(ref_image_bytes).decode("ascii")
    body = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": ref_mime, "data": ref_b64}},
            ],
        }],
        "generationConfig": {
            "responseModalities": ["IMAGE", "TEXT"],
        },
    }
    last_err = ""
    for model in GEMINI_MODELS:
        url = GEMINI_URL.format(model=model, key=GEMINI_API_KEY)
        try:
            r = requests.post(url, json=body, timeout=120)
        except Exception as e:
            last_err = f"{model}: exc {type(e).__name__}: {e}"
            continue
        if r.status_code != 200:
            last_err = f"{model}: status={r.status_code} body={r.text[:200]}"
            continue
        data = r.json()
        # parts[].inline_data.data 추출
        try:
            parts = data["candidates"][0]["content"]["parts"]
            for p in parts:
                inline = p.get("inline_data") or p.get("inlineData")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])
            last_err = f"{model}: no inline_data in response — keys={list(data.keys())}"
        except Exception as e:
            last_err = f"{model}: parse err {e}"
    print(f"    gemini fail: {last_err[:300]}")
    return None


def process_one(target: tuple) -> dict:
    product_id, seller_product_id, asin, title_ko, prompt_kind = target
    out_dir = DL_DIR / product_id
    orig_dir = out_dir / "original"
    new_dir = out_dir / "new"
    orig_dir.mkdir(parents=True, exist_ok=True)
    new_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "productId": product_id,
        "sellerProductId": seller_product_id,
        "asin": asin,
        "title_ko": title_ko,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "images": [],
    }

    print(f"\n=== {title_ko[:40]} (productId={product_id}) ===")
    sp = get_seller_product(seller_product_id)
    if not sp:
        info["error"] = "seller product 조회 실패"
        (out_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
        return info

    images = extract_image_urls(sp)
    print(f"  쿠팡 이미지: {len(images)}장")
    if not images:
        info["error"] = "이미지 URL 없음"
        (out_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
        return info

    prompt = PROMPT_BASE + " " + PROMPT_KIND_HINTS.get(prompt_kind, "")
    info["prompt"] = prompt

    for i, (img_type, url) in enumerate(images):
        ext = ".jpg" if url.lower().endswith(".jpg") or url.lower().endswith(".jpeg") else ".png"
        orig_path = orig_dir / f"{i:02d}_{img_type}{ext}"
        new_path = new_dir / f"{i:02d}_{img_type}{ext}"

        print(f"  [{i}] {img_type} ← {url[:70]}")
        if not download(url, orig_path):
            info["images"].append({"idx": i, "type": img_type, "url": url, "status": "download_fail"})
            continue

        new_img = gemini_generate_image(prompt, orig_path.read_bytes(),
                                        "image/jpeg" if ext == ".jpg" else "image/png")
        if new_img:
            new_path.write_bytes(new_img)
            info["images"].append({
                "idx": i, "type": img_type, "url": url,
                "original": str(orig_path.relative_to(out_dir)),
                "new": str(new_path.relative_to(out_dir)),
                "status": "ok",
            })
            print(f"      → new: {new_path.name} ({len(new_img)} bytes)")
        else:
            info["images"].append({
                "idx": i, "type": img_type, "url": url,
                "original": str(orig_path.relative_to(out_dir)),
                "status": "generate_fail",
            })
        time.sleep(2)  # Gemini rate limit

    (out_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
    n_ok = sum(1 for x in info["images"] if x.get("status") == "ok")
    print(f"  → {n_ok}/{len(images)} 신규 생성 / 저장: {out_dir}")
    return info


def main():
    print(f"출력: {DL_DIR}")
    if not DL_DIR.exists():
        print(f"폴더 없음 — 생성합니다.")
        DL_DIR.mkdir(parents=True, exist_ok=True)

    # 인자 처리 — 일부 productId 만 처리
    selected = sys.argv[1:] if len(sys.argv) > 1 else None
    if selected:
        targets = [t for t in TARGETS if t[0] in selected]
        if not targets:
            print(f"매칭 productId 없음: {selected}")
            return
    else:
        targets = TARGETS

    print(f"대상: {len(targets)}건")
    results = [process_one(t) for t in targets]
    print(f"\n=== 완료 — {len(results)}건 처리 ===")
    for r in results:
        n_ok = sum(1 for x in r.get("images", []) if x.get("status") == "ok")
        n_total = len(r.get("images", []))
        print(f"  {r['productId']} ({r['title_ko'][:30]}...): {n_ok}/{n_total} ok")


if __name__ == "__main__":
    main()
