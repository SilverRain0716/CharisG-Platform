# -*- coding: utf-8 -*-
"""상세페이지 인포그래픽 렌더러 (PIL + Gemini 텍스트 추출).

build_payload(coupang_lister)가 상세 contents 에 삽입.
구조: 색상 배너(카테고리 테마색) + 상품명 + 구성품(실사 크롭/대표사진) + 특징 카드.

★캐시 원칙 — relist 안전성/비용 0:
  Gemini 텍스트 추출 결과는 media/products/{pid}/meta.json 에 1회 캐시.
  완성 이미지는 media/products/{pid}/infographic.jpg 에 캐시.
  render_infographic(force=False) 는 파일이 있으면 재생성 없이 URL 만 반환 →
  매 리스트/relist(콘텐츠 수정=고위험 재심사)마다 Gemini 재호출/이미지 변동 방지.
  detailing 단계에서 force=True 로 1회 생성(prewarm)하는 것이 정석.

★실사 구성품(선택, 수동 ~20%): media/products/{pid}/crops.json
  {"source_idx":1,"counts":{"보석":"60"},"boxes":{"보석":[x0,y0,x1,y1]}}  # 0~1 비율
  source_idx = image_cache.image_idx (크롭 원본). 없으면 대표사진(MAIN)+구성 캡션 폴백.
"""
import os
import io
import re
import json
import logging
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter

logger = logging.getLogger(__name__)

_ASSETS = Path(__file__).resolve().parent.parent / "assets"
_MEDIA = Path(__file__).resolve().parent.parent / "media"
_FONT_CANDIDATES = [
    str(_ASSETS / "NanumGothic.ttf"),
    "/home/ubuntu/fonts/NanumGothic.ttf",
]
PAL = [(13, 148, 136), (37, 99, 235), (217, 119, 6),
       (190, 24, 93), (5, 150, 105), (124, 58, 237)]


def _font(sz):
    for p in _FONT_CANDIDATES:
        if os.path.isfile(p):
            return ImageFont.truetype(p, sz)
    return ImageFont.load_default()


def _gemini_key():
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def _gemini_keys():
    ks = []
    for n in ("GEMINI_API_KEY", "GEMINI_API_KEY_FALLBACK", "GEMINI_API_KEY_5",
              "GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GOOGLE_API_KEY"):
        v = os.environ.get(n)
        if v and v not in ks:
            ks.append(v)
    return ks


def _gtext(prompt):
    """Gemini 텍스트 생성 (실패 시 빈 문자열).
    ★flash RPD/월지출한도 소진(2026-07-01) → 별도 쿼터버킷 flash-lite 우선 + 키 로테이션.
      기존: 단일키·flash 고정 → 소진 시 그룹 상세생성이 429/지연으로 램프 병목."""
    keys = _gemini_keys()
    if not keys:
        logger.warning("[infographic] GEMINI_API_KEY 없음 — 텍스트 추출 skip")
        return ""
    for m in ("gemini-2.5-flash-lite", "gemini-2.0-flash"):
        for key in keys:
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={key}",
                    json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
                if r.status_code == 200:
                    return r.json()["candidates"][0]["content"]["parts"][0]["text"]
                if r.status_code != 429:
                    break   # 비-429(400 등)는 같은 모델 다른 키도 무의미 → 다음 모델로
            except Exception as e:
                logger.warning(f"[infographic] gtext {m} 실패: {str(e)[:80]}")
    return ""


def _extract_meta(asin, title):
    """SP-API bullet → Gemini → 상세 메타 JSON (효능 가드레일 포함). 3회 재시도."""
    from backend.purchase.services.image_downloader import fetch_product_info_sp_api
    try:
        bullets = fetch_product_info_sp_api(asin).get("bullet_points") or []
    except Exception as e:
        logger.warning(f"[infographic] bullet 조회 실패 {asin}: {str(e)[:80]}")
        bullets = []
    PR = (
        '상품 특징(영문)→한국 쇼핑몰 상세 JSON. 형식:\n'
        '{"tagline":"상품 한줄홍보 한글(20자내)",'
        '"product_type":"책/미디어 | 건강식품 | 일반 중 하나 '
        '(책/미디어는 책·오디오북·잡지 등 콘텐츠 자체일 때만. 음반 슬리브·케이스·거치대 등 미디어 용품은 일반)",'
        '"theme_color":"#RRGGBB (상품 카테고리에 어울리는 대표색 hex 한 개. '
        '주방:#d97706 / 뷰티:#db2777 / 스포츠·아웃도어:#059669 / 전자·공구·차량:#2563eb / '
        '책:#1e3a8a / 완구:#7c3aed / 건강식품:#0d9488)",'
        '"single_item":true/false(여러 개 구성된 세트면 false, 단일 품목이면 true),'
        '"components":[{"name":"구성품(6자내)","count":"수량숫자"}],'
        '"features":[{"head":"제목(12자내)","sub":"설명(26자내)"}]}\n'
        '★중요 규칙(반드시 준수):\n'
        '1) 효능·효과·치료·의학적 표현 절대 금지(약사법·표시광고법): "노화 관리/세포 재생/'
        '면역 강화/질병 개선/대사 개선/항산화 효과" 같은 표현 쓰지 말 것. 객관적 사실·구성·소재·용도만.\n'
        '2) product_type이 "책/미디어"면 features는 그 제품의 효능이 아니라 "책 내용/주제/대상 독자"로 '
        '작성. 제품이 효능을 준다고 절대 표현 금지.\n'
        '3) 건강식품도 효능 단정 금지 — "함유 성분/용량/섭취 방법" 등 사실만.\n'
        'single_item이면 components 빈 배열. components 최대4, features 최대6. JSON만 출력.\n제목:'
        + (title or "") + '\n특징:\n- ' + '\n- '.join(bullets))
    for _ in range(3):
        raw = _gtext(PR)
        try:
            d = json.loads(raw[raw.find("{"):raw.rfind("}") + 1]) if (raw and "{" in raw) else {}
        except Exception:
            d = {}
        if d.get("features"):
            return d
    return {}


def _classify_plain(im):
    """플레인 제품컷(흰배경+저엣지) 여부 — 연출/인포그래픽 사진 제외."""
    s = im.resize((220, 220))
    px = s.load()
    bd = [px[x, y] for x in range(220) for y in range(220)
          if x < 18 or x > 201 or y < 18 or y > 201]
    bb = sum(sum(c) / 3 for c in bd) / len(bd)
    bstd = (sum((sum(c) / 3 - bb) ** 2 for c in bd) / len(bd)) ** 0.5
    e = s.convert("L").filter(ImageFilter.FIND_EDGES).load()
    ec = sum(1 for x in range(220) for y in range(220) if e[x, y] > 60) / (220 * 220)
    return bstd <= 36 and ec <= 0.105


def _cache_rows(product_id):
    from backend.purchase.database import get_db
    with get_db() as c:
        return c.execute(
            "SELECT image_idx, local_path, public_url FROM image_cache "
            "WHERE product_id=? AND public_url IS NOT NULL ORDER BY image_idx ASC",
            (product_id,)).fetchall()


def _abs_url(public_url):
    from backend_shared._config import PUBLIC_BASE_URL
    base = PUBLIC_BASE_URL.rstrip("/")
    return public_url if public_url.startswith("http") else f"{base}{public_url}"


def select_detail_images(product_id, max_n=5):
    """상세 contents 용 이미지 선별: MAIN(idx0, 항상) + 플레인 제품컷 ≤(max_n-1).
    절대 URL 리스트 반환. 분류 불가/빈 결과면 호출부가 폴백."""
    rows = _cache_rows(product_id)
    # ★마케팅 그래픽 제외(2026-06-29): 기존엔 _classify_plain(흰배경)만 봐서 마케팅 인포그래픽이
    #   상세에 누출됐음(콜맨 의자 "Comfortable" 텍스트 콜아웃컷). image_classifier 로 marketing 제외.
    try:
        from backend.purchase.services.image_classifier import classify_images
        _mk_cls = classify_images(product_id)
    except Exception:
        _mk_cls = {}
    sel = []
    for r in rows:
        lp = r["local_path"]
        if _mk_cls.get(lp, "photo") != "photo":
            continue   # marketing(그래픽)+lifestyle(인물/실사용환경) 상세 제외
        is_main = (r["image_idx"] == 0) or (not sel)
        plain = is_main
        if not is_main and lp and os.path.isfile(lp):
            try:
                with Image.open(lp) as im:
                    plain = _classify_plain(im.convert("RGB"))
            except Exception:
                plain = False
        if is_main or (len(sel) < max_n and plain):
            sel.append(_abs_url(r["public_url"]))
        if len(sel) >= max_n:
            break
    return sel


def _trim(c, thr=238):
    """흰 여백 자동 트림."""
    g = c.convert("L")
    px = g.load()
    w, h = c.size
    xs, ys = [], []
    for yy in range(0, h, 4):
        for xx in range(0, w, 4):
            if px[xx, yy] < thr:
                xs.append(xx)
                ys.append(yy)
    if not xs:
        return c
    p = 12
    return c.crop((max(0, min(xs) - p), max(0, min(ys) - p),
                   min(w, max(xs) + p), min(h, max(ys) + p)))


def _whiten(im, thr=232):
    im = im.convert("RGB")
    px = im.load()
    w, h = im.size
    for yy in range(h):
        for xx in range(w):
            r, g, b = px[xx, yy]
            if r > thr and g > thr and b > thr:
                px[xx, yy] = (255, 255, 255)
    return im


def _soft_card(canvas, box, radius, fill):
    sh = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle(
        [box[0] + 2, box[1] + 10, box[2] + 2, box[3] + 12], radius=radius, fill=(20, 40, 60, 75))
    canvas.alpha_composite(sh.filter(ImageFilter.GaussianBlur(11)))
    ImageDraw.Draw(canvas).rounded_rectangle(box, radius=radius, fill=fill)


def _wrap(s, n=23, maxl=2):
    out, cur = [], ""
    for w in (s or "").split():
        if len(cur) + len(w) + 1 <= n:
            cur = (cur + " " + w).strip()
        else:
            if cur:
                out.append(cur)
            cur = w
        if len(out) >= maxl:
            break
    if cur and len(out) < maxl:
        out.append(cur)
    return out[:maxl] or [(s or "")[:n]]


def render_infographic(product_id, force=False):
    """인포그래픽 JPG 렌더 → 상대 URL 반환. 내용 없거나 실패면 None.
    캐시: media/products/{pid}/infographic.jpg 있으면 재생성 없이 URL 반환(force=False)."""
    out_dir = _MEDIA / "products" / str(product_id)
    out = out_dir / "infographic.jpg"
    url = f"/api/pa/images/products/{product_id}/infographic.jpg"
    if out.exists() and not force:
        return url

    from backend.purchase.database import get_db
    with get_db() as c:
        p = c.execute("SELECT asin, title_ko FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        return None
    asin = p["asin"]
    title = (p["title_ko"] or asin or "").strip()

    out_dir.mkdir(parents=True, exist_ok=True)
    # ── Gemini 메타 (캐시) ──
    meta_f = out_dir / "meta.json"
    data = {}
    if meta_f.exists():
        try:
            data = json.loads(meta_f.read_text())
        except Exception:
            data = {}
    if not data.get("features"):
        data = _extract_meta(asin, title)
        if data.get("features"):
            meta_f.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    feats = (data.get("features") or [])[:6]
    comps = (data.get("components") or [])[:4]
    if data.get("single_item") or len(comps) <= 1:
        comps = []
    if not feats and not comps:
        return None  # 표시할 내용 없음 → 인포그래픽 생략

    # ── 테마색 ──
    tc = (data.get("theme_color") or "").strip()
    if not re.match(r"^#[0-9a-fA-F]{6}$", tc):
        tc = "#0d9488"
    THEME = tuple(int(tc[i:i + 2], 16) for i in (1, 3, 5))

    # ── 이미지 (image_cache) ──
    rows = _cache_rows(product_id)

    def _load(idx):
        for r in rows:
            if r["image_idx"] == idx and r["local_path"] and os.path.isfile(r["local_path"]):
                try:
                    return Image.open(r["local_path"]).convert("RGB")
                except Exception:
                    return None
        return None

    rep = _load(0)  # 대표사진(MAIN)

    # ── 실사 구성품 크롭 (crops.json, 선택) ──
    real = {}
    cj = out_dir / "crops.json"
    if cj.exists():
        try:
            cfg = json.loads(cj.read_text())
            src = _load(int(cfg.get("source_idx", 0)))
            if src:
                W0, H0 = src.size
                for nm, bx in cfg.get("boxes", {}).items():
                    cr = src.crop((int(bx[0] * W0), int(bx[1] * H0),
                                   int(bx[2] * W0), int(bx[3] * H0)))
                    real[nm] = _trim(cr)
                comps = [{"name": nm, "count": cfg.get("counts", {}).get(nm, "")}
                         for nm in cfg.get("boxes", {})]
        except Exception as e:
            logger.warning(f"[infographic] crops.json 처리 실패 pid={product_id}: {str(e)[:80]}")

    # ── 렌더 ──
    W = 760
    heroH = 200
    has_cimg = bool(real)
    REPH = 420
    ccard = 312
    if comps and has_cimg:
        crow = (len(comps) + 1) // 2
        comp_sec = 56 + crow * (ccard + 18)
    elif comps and rep:
        crow = 0
        comp_sec = 56 + REPH
    else:
        crow = 0
        comp_sec = 0
    frow = (len(feats) + 1) // 2
    feat_sec = (56 + frow * 150) if feats else 0
    H = heroH + 24 + comp_sec + feat_sec + 30
    ig = Image.new("RGBA", (W, H), (248, 250, 252, 255))
    d = ImageDraw.Draw(ig)

    # 색상 그라데이션 배너
    c1 = THEME
    c2 = tuple(max(0, int(c * 0.55)) for c in THEME)
    for yy in range(heroH):
        t = yy / heroH
        d.line([(0, yy), (W, yy)],
               fill=tuple(int(c1[k] + (c2[k] - c1[k]) * t) for k in range(3)) + (255,))
    # product_type이 "책/미디어"여도 음반 슬리브·케이스 등 '미디어 용품'엔 책 제목 부적절.
    # 제목에 용품 키워드가 있으면 일반 제목으로 폴백(실제 책·오디오북만 "다루는 내용").
    _pt = data.get("product_type") or ""
    _acc_kw = ("슬리브", "케이스", "커버", "홀더", "거치", "스탠드", "보관", "정리",
               "받침", "케이블", "어댑터", "프로텍터", "sleeve", "case", "holder", "stand")
    is_book = ("책" in _pt) and not any(k in (title or "").lower() for k in _acc_kw)
    FEAT_TITLE = "이 책에서 다루는 내용" if is_book else "이런 점이 좋아요"
    lines = _wrap(title)
    ty = 44 if len(lines) > 1 else 62
    for ln in lines:
        d.text((40, ty), ln, font=_font(31), fill=(255, 255, 255, 255))
        ty += 42
    d.text((40, heroH - 42), (data.get("tagline") or "")[:34], font=_font(20),
           fill=(255, 255, 255, 205))
    y = heroH + 24

    # 구성품
    if comps and has_cimg:
        d.text((40, y), "구 성 품", font=_font(26), fill=(30, 41, 59, 255))
        y += 50
        gap = 18
        cw = (W - 80 - gap) // 2
        imgH = ccard - 66
        for i, c in enumerate(comps):
            nm = c.get("name", "")
            col = PAL[i % len(PAL)]
            cx = 40 + (i % 2) * (cw + gap)
            cy = y + (i // 2) * (ccard + gap)
            _soft_card(ig, [cx, cy, cx + cw, cy + ccard], 22, (255, 255, 255, 255))
            if nm in real:
                th = real[nm].copy()
                th.thumbnail((cw - 56, imgH - 28))
                th = _whiten(th)
                ig.alpha_composite(th.convert("RGBA"),
                                   (cx + (cw - th.width) // 2, cy + 14 + (imgH - th.height) // 2))
            fy = cy + imgH + 6
            dd = ImageDraw.Draw(ig)
            dd.rounded_rectangle([cx, fy, cx + cw, cy + ccard], radius=22, fill=col + (255,))
            dd.rectangle([cx, fy, cx + cw, fy + 22], fill=col + (255,))
            dd.text((cx + 22, fy + 14), nm[:9], font=_font(24), fill=(255, 255, 255, 255))
            cnt = str(c.get("count", "")).strip()
            if cnt:
                dd.text((cx + cw - 22, fy + 10), cnt + ("개" if cnt.isdigit() else ""),
                        font=_font(28), fill=(255, 255, 255, 255), anchor="ra")
        y += crow * (ccard + gap) + 6
    elif comps and rep:
        d.text((40, y), "구 성 품", font=_font(26), fill=(30, 41, 59, 255))
        y += 50
        _soft_card(ig, [40, y, W - 40, y + REPH], 22, (255, 255, 255, 255))
        mh = REPH - 64
        mi = _whiten(rep.copy())
        mi.thumbnail((W - 130, mh - 24))
        ig.alpha_composite(mi.convert("RGBA"), ((W - mi.width) // 2, y + 14 + (mh - mi.height) // 2))
        fy = y + mh + 6
        dd = ImageDraw.Draw(ig)
        col = PAL[0]
        dd.rounded_rectangle([40, fy, W - 40, y + REPH], radius=22, fill=col + (255,))
        dd.rectangle([40, fy, W - 40, fy + 22], fill=col + (255,))
        cap = " · ".join(
            (c.get("name", "") + (" " + str(c.get("count", "")).strip() + "개"
             if str(c.get("count", "")).strip().isdigit() else "")) for c in comps)
        dd.text((58, fy + 12), ("구성  " + cap)[:42], font=_font(22), fill=(255, 255, 255, 255))
        y += REPH + 6

    # 특징 카드
    if feats:
        d = ImageDraw.Draw(ig)
        d.text((40, y), FEAT_TITLE, font=_font(26), fill=(30, 41, 59, 255))
        y += 50
        cw2 = (W - 80 - 16) // 2
        for i, f in enumerate(feats):
            col = PAL[i % len(PAL)]
            cx = 40 + (i % 2) * (cw2 + 16)
            cy = y + (i // 2) * 150
            _soft_card(ig, [cx, cy, cx + cw2, cy + 134], 16,
                       tuple(int(col[k] * 0.10 + 255 * 0.90) for k in range(3)) + (255,))
            dd = ImageDraw.Draw(ig)
            dd.ellipse([cx + 20, cy + 22, cx + 62, cy + 64], fill=col + (255,))
            dd.text((cx + 41, cy + 30), str(i + 1), font=_font(24),
                    fill=(255, 255, 255, 255), anchor="ma")
            dd.text((cx + 78, cy + 24), str(f.get("head", ""))[:13], font=_font(23),
                    fill=(20, 20, 20, 255))
            sub = str(f.get("sub", ""))
            dd.text((cx + 24, cy + 74), sub[:18], font=_font(17), fill=(90, 90, 90, 255))
            if len(sub) > 18:
                dd.text((cx + 24, cy + 98), sub[18:36], font=_font(17), fill=(90, 90, 90, 255))

    ig = ig.convert("RGB")
    ig.save(str(out), "JPEG", quality=92, optimize=True)
    return url
