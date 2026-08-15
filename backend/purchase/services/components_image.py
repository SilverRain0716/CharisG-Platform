# -*- coding: utf-8 -*-
"""구성품 가공컷 + 구성품 설명 블록 — 세트/번들 상품.
   - components_cut.jpg : Nano Banana 재생성(구성품 전체 깨끗한 자체이미지, 저작권 안전)
   - components_block.jpg: included_components 파싱 → 항목별 그리드(스펙표와 별개), 2종 이상만
   build_payload/build_detail_contents 가 두 파일 존재 시 '구성품' 영역에 삽입(가공컷→설명블록 순)."""
import os, json, base64, time, re, logging
from io import BytesIO
from pathlib import Path
import requests
from PIL import Image as PI, ImageDraw, ImageFont
from backend.purchase.database import get_db

logger = logging.getLogger(__name__)
_MEDIA = Path(__file__).resolve().parent.parent / "media"
_ASSETS = Path(__file__).resolve().parent.parent / "assets"
_GEN_MODEL = "gemini-2.5-flash-image"
_PROMPT = (
    "Premium e-commerce product photographer. Use the provided image ONLY as reference for the items' "
    "true appearance (shape, color, material faithful and unchanged). Generate a NEW clean studio photo "
    "showing ALL the individual items included in this set/bundle, neatly arranged together so every "
    "included component is clearly visible and countable. ABSOLUTELY NO logos, NO brand marks, NO emblems, "
    "NO text, letters or numbers anywhere on the products or image. STRICTLY EXCLUDE packaging, boxes, "
    "retail labels, price tags, stickers. Clean minimal soft-gradient background, professional soft "
    "lighting, subtle shadow, generous margin, high-end catalog look. No watermarks, no people, no hands."
)
_FONT_CANDIDATES = [str(_ASSETS / "NanumGothic.ttf"), "/home/ubuntu/fonts/NanumGothic.ttf"]
_ACC = (31, 110, 67); _DARK = (26, 29, 34); _SUB = (107, 112, 121); _ROWBG = (247, 248, 249)


def _font(sz):
    for p in _FONT_CANDIDATES:
        if os.path.isfile(p):
            return ImageFont.truetype(p, sz)
    return ImageFont.load_default()


def _gemini_keys():
    ks = []
    for n in ("GEMINI_API_KEY_5", "GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3",
              "GEMINI_API_KEY_4", "GEMINI_API_KEY_FALLBACK"):
        v = os.environ.get(n)
        if v and v not in ks:
            ks.append(v)
    return ks


def _raw_attr0(attrs, key):
    v = attrs.get(key)
    return v[0].get("value") if isinstance(v, list) and v and isinstance(v[0], dict) else None


def is_set_product(pid):
    """(is_set, components_text). sp_raw_json 의 included_components/set_name/number_of_items>1."""
    with get_db() as c:
        r = c.execute("SELECT sp_raw_json FROM products WHERE id=?", (pid,)).fetchone()
    if not r or not r["sp_raw_json"]:
        return False, None
    try:
        attrs = (json.loads(r["sp_raw_json"]) or {}).get("attributes") or {}
    except Exception:
        return False, None
    inc = _raw_attr0(attrs, "included_components")
    setn = _raw_attr0(attrs, "set_name")
    noi = _raw_attr0(attrs, "number_of_items")
    try:
        noi_n = int(float(noi)) if noi is not None else 0
    except (TypeError, ValueError):
        noi_n = 0
    return (bool(inc) or bool(setn) or noi_n > 1), (inc or setn)


def _split_top_level(txt):
    """괄호( ( [ { ) 밖의 , ; 로만 분리 — 괄호 안 쉼표는 구분자가 아님(보존).
    예 "4 Toys (Duck, Hare, Squirrel, Alligator)" 가 4조각+덜렁괄호로 깨지던 버그 방지."""
    parts, buf, depth = [], "", 0
    for ch in (txt or ""):
        if ch in "([{":
            depth += 1; buf += ch
        elif ch in ")]}":
            depth = max(0, depth - 1); buf += ch
        elif ch in ",;" and depth == 0:
            parts.append(buf); buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    return parts


def _parse_components(txt):
    out = []
    for part in _split_top_level(txt or ""):
        s = part.strip()
        if not s:
            continue
        m = re.match(r"(\d+)\s*[\*xX×]\s*(.+)", s) or re.match(r"(\d+)\s+(.+)", s)
        if m:
            qty, name = int(m.group(1)), m.group(2).strip()
            # ★소재 등급(304/316 스테인리스·6061 알루미늄·게이지 등)·비정상 수량 오파싱 방지:
            #   수량은 1~50 만 인정, 그 외엔 숫자를 이름의 일부로 되돌림 (예 "304 Stainless..." ×304 버그).
            if qty > 50 or qty <= 0 or name.lower().startswith(("stainless", "steel", "alumin", "gauge")):
                qty, name = 1, s
        else:
            qty, name = 1, s
        # ★괄호 끝 목록 전개(2026-06-30): "<N> <base> (a, b, c, d)" 에서 괄호 안 쉼표목록 개수==N(≥2)이면
        #   괄호목록을 개별 구성품(각 ×1)으로 전개. 예 "4 Stuffless Plush Toys (Wild Duck, Hare, Squirrel,
        #   Alligator)" → 오리·토끼·다람쥐·악어 ×1. (개수≠수량이면 전개 안 함 = '16 oz, BPA free' 같은 설명 보존)
        mp = re.match(r"^(.*?)\s*\(([^()]+)\)\s*$", name)
        if mp:
            inner = [x.strip() for x in mp.group(2).split(",") if x.strip()]
            if len(inner) >= 2 and qty == len(inner):
                for it in inner:
                    out.append(((it[:1].upper() + it[1:]) if it else it, 1))
                continue
        name = (name[:1].upper() + name[1:]) if name else name
        out.append((name, qty))
    return out


def render_components_block(pid, comp_text=None):
    """구성품 설명 블록(PIL). 2종 이상일 때만 components_block.jpg 생성, public path 반환(아니면 None)."""
    if comp_text is None:
        _, comp_text = is_set_product(pid)
    items = _parse_components(comp_text)
    if len(items) < 2:
        return None   # 단일 품목은 블록 생략
    W, PAD, row_h, head_h = 1080, 84, 92, 250
    cols = 2 if len(items) > 4 else 1
    rows_n = (len(items) + cols - 1) // cols
    H = head_h + rows_n * row_h + 80
    im = PI.new("RGB", (W, H), "#ffffff")
    d = ImageDraw.Draw(im)
    d.line([(PAD, 96), (PAD + 46, 96)], fill=_ACC, width=3)
    d.text((PAD + 58, 86), "SET INCLUDES", font=_font(22), fill=_ACC)
    d.text((PAD, 120), "구성품 구성", font=_font(60), fill=_DARK)
    # ★수량 표시 제거(2026-06-30): included_components 원본이 지저분해 수량/총개수 파싱이 불안정
    #   ("2 pack ..."·설명문장·치수숫자) → 헤더 "총 N개" 와 행별 ×N 배지를 빼고 품목 리스트만 노출.
    d.text((PAD, 196), f"구성품 {len(items)}종", font=_font(26), fill=_SUB)
    gw = (W - PAD * 2 - 28) // cols
    for i, (name, qty) in enumerate(items):
        c = i % cols; r = i // cols
        x = PAD + c * (gw + 28); y = head_h + r * row_h
        d.rounded_rectangle([x, y, x + gw, y + row_h - 16], radius=16, fill=_ROWBG)
        cy = y + (row_h - 16) // 2
        d.ellipse([x + 22, cy - 15, x + 52, cy + 15], fill=_ACC)
        d.line([(x + 30, cy), (x + 36, cy + 7)], fill="#fff", width=3)
        d.line([(x + 36, cy + 7), (x + 46, cy - 6)], fill="#fff", width=3)
        nf = _font(28); name_x = x + 70; name_max = x + gw - name_x - 28   # ★배지 제거 → 이름이 전폭 사용
        nm = name
        while nm and d.textlength(nm + "…", font=nf) > name_max:
            nm = nm[:-1]
        if nm != name and nm:
            nm = nm.rstrip() + "…"
        d.text((name_x, cy - 16), nm or name, font=nf, fill=_DARK)
    md = _MEDIA / "products" / str(pid)
    md.mkdir(parents=True, exist_ok=True)
    im.save(md / "components_block.jpg", "JPEG", quality=88)
    return f"/api/pa/images/products/{pid}/components_block.jpg"


def _gen(ref_local_path, out_path):
    b = base64.b64encode(open(ref_local_path, "rb").read()).decode()
    body = {"contents": [{"parts": [{"text": _PROMPT}, {"inline_data": {"mime_type": "image/jpeg", "data": b}}]}],
            "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]}}
    base_url = f"https://generativelanguage.googleapis.com/v1beta/models/{_GEN_MODEL}:generateContent?key="
    for rnd in range(2):
        for k in _gemini_keys():
            try:
                r = requests.post(base_url + k, json=body, timeout=180)
                if r.status_code == 200:
                    for p in r.json()["candidates"][0]["content"]["parts"]:
                        inl = p.get("inline_data") or p.get("inlineData")
                        if inl and inl.get("data"):
                            im = PI.open(BytesIO(base64.b64decode(inl["data"]))).convert("RGB")
                            if im.width != 1080:
                                im = im.resize((1080, int(im.height * 1080 / im.width)))
                            im.save(out_path, "JPEG", quality=88)
                            return True
                else:
                    logger.info(f"[components] gen status {r.status_code} → 키전환")
            except Exception as e:
                logger.info(f"[components] gen 예외 {str(e)[:40]}")
            time.sleep(1.5)
    return False


def ensure_components_cut(pid, force=False):
    """included_components 가 '진짜 2종 이상'일 때만 ①설명블록(PIL) + ②가공컷(Nano Banana) 둘 다 생성.
    2종 미만(단품/동일품목 N개/모델코드 등)은 둘 다 생성 안 함(일관). 가공컷 public path 반환."""
    _, comp_text = is_set_product(pid)
    items = _parse_components(comp_text) if comp_text else []
    if len(items) < 2:
        return None
    md = _MEDIA / "products" / str(pid)
    try:
        render_components_block(pid, comp_text)
    except Exception as e:
        logger.info(f"[components] block 렌더 실패 {pid}: {e}")
    out = md / "components_cut.jpg"
    if out.is_file() and not force:
        return f"/api/pa/images/products/{pid}/components_cut.jpg"
    try:
        from backend.purchase.services.coupang_lister import _get_product_images
        imgs = _get_product_images(pid)
    except Exception:
        imgs = []
    if not imgs:
        return None
    ref = str(md / imgs[0].rsplit("/", 1)[-1])
    if not os.path.isfile(ref):
        return None
    md.mkdir(parents=True, exist_ok=True)
    if _gen(ref, str(out)):
        logger.info(f"[components] product {pid} 구성품 가공컷 생성")
        return f"/api/pa/images/products/{pid}/components_cut.jpg"
    return None
