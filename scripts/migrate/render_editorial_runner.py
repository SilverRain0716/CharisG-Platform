# -*- coding: utf-8 -*-
"""공유 에디토리얼 렌더러 (B 재설계) — master pid 1개에 대해 vision 1세트 + 사진-free 섹션 렌더.
출력: media/products/{pid}/edsec_{i}.jpg + ed_manifest.json([/api/pa/... URL]).
build_detail_contents(B모드)가 옵션 전부에 이 공유 섹션을 재사용 → vision 부모당 1회.
서브프로세스 격리(Playwright/전역). usage: python3 render_editorial_runner.py <master_pid>
"""
import os, sys, json, base64, re, asyncio, sqlite3, requests, html, time
from pathlib import Path
from dotenv import load_dotenv
from PIL import Image as PI

BASE = Path.home() / "CharisG-Platform/charisg-platform"
load_dotenv(BASE / ".env")
DB = BASE / "backend/purchase/purchase.db"
KEY = os.environ.get("GEMINI_API_KEY", "")
VIS = "gemini-2.5-flash-lite"  # 전상품 에디토리얼 전환(2026-07-05): flash 소진회피+저비용
URL = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key=" + KEY
ACCENT = "#1f6e43"
PID = sys.argv[1]
_TEXTONLY = len(sys.argv) > 2 and sys.argv[2] == "textonly"  # 공유 텍스트-only 에디토리얼(그룹옵션 공유용)
MEDIA = BASE / "backend/purchase/media/products" / str(PID)


def E(s): return html.escape(str(s or ""))


CSS = """
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.css');
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Playfair+Display:wght@500;600;700&display=swap');
*{box-sizing:border-box;margin:0;padding:0;}
body{width:1080px;background:#fff;font-family:'Pretendard',sans-serif;-webkit-font-smoothing:antialiased;color:#16181d;}
.sec{position:relative;width:1080px;padding:104px 84px;background:#fff;overflow:hidden;}
.sec.soft{background:#F7F6F2;}
.snum{position:absolute;top:60px;right:72px;font-family:'Playfair Display',serif;font-size:130px;font-weight:600;color:#f1f0ea;line-height:1;letter-spacing:-.02em;z-index:0;}
.sec.soft .snum{color:#eeede6;}
.inner{position:relative;z-index:1;}
.eyebrow{display:flex;align-items:center;gap:14px;font-family:'Inter';text-transform:uppercase;letter-spacing:.24em;font-size:16px;font-weight:600;color:var(--acc);}
.eyebrow::before{content:"";width:30px;height:2px;background:var(--acc);display:inline-block;}
.h-en{font-family:'Playfair Display',serif;font-size:52px;font-weight:600;letter-spacing:-.015em;color:#13151a;margin-top:18px;line-height:1.08;}
.h-ko{font-size:23px;color:#6b7079;margin-top:12px;font-weight:500;letter-spacing:.01em;}
.cmp{width:100%;border-collapse:separate;border-spacing:0;margin-top:52px;}
.cmp th{padding:18px 20px;font-family:'Inter';font-size:14px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;text-align:left;}
.cmp th.ours{background:var(--acc);color:#fff;border-radius:14px 14px 0 0;text-align:center;}
.cmp th.oth{color:#b3b8bf;text-align:center;}
.cmp td{padding:22px 20px;border-bottom:1px solid #edeeef;vertical-align:middle;}
.cmp tr:last-child td{border-bottom:none;}
.cmp td.pt b{font-size:18.5px;color:#1d2026;font-weight:600;display:block;letter-spacing:-.01em;}
.cmp td.pt span{font-size:13px;color:#7c818a;font-weight:500;}
.cmp td.ours{background:#fbfbf9;}
.cmp td.ours.last{border-radius:0 0 14px 14px;}
.cmp .v{display:flex;align-items:center;gap:12px;font-size:15px;}
.cmp .v.y{color:#2b2f36;font-weight:500;}
.cmp .v.n{color:#b7bcc3;}
.sic{width:23px;height:23px;flex:0 0 23px;}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-top:50px;}
.card{position:relative;background:#fff;border:1px solid #eceef0;border-radius:22px;padding:34px 32px 32px;box-shadow:0 10px 30px rgba(20,28,38,.05);}
.card .num{font-family:'Inter';font-weight:700;font-size:19px;color:var(--acc);letter-spacing:.1em;}
.card .nen{font-size:34px;font-weight:600;color:#191c22;margin-top:10px;letter-spacing:-.01em;}
.card .nko{font-size:21px;color:#6f757e;margin-top:4px;font-weight:500;}
.card .den{font-size:25px;color:#565c66;margin-top:18px;line-height:1.62;}
.card .dko{font-size:24px;color:#565b63;margin-top:9px;line-height:1.72;}
.card .bar{position:absolute;top:34px;right:32px;width:34px;height:3px;border-radius:2px;background:var(--acc);opacity:.85;}
.phcard{background:#fff;border-radius:30px;box-shadow:0 16px 44px rgba(20,28,38,.07);margin-top:46px;padding:64px 56px;display:flex;align-items:center;justify-content:center;min-height:520px;}
.phcard img{max-width:100%;max-height:760px;width:auto;height:auto;object-fit:contain;display:block;}
.steps{display:flex;gap:24px;margin-top:54px;}
.step{flex:1;background:#fff;border:1px solid #ececec;border-radius:22px;padding:36px 28px 32px;box-shadow:0 10px 30px rgba(20,28,38,.05);position:relative;}
.step .sn{width:58px;height:58px;border-radius:16px;background:var(--acc);color:#fff;font-family:'Inter';font-weight:700;font-size:24px;display:flex;align-items:center;justify-content:center;margin-bottom:22px;}
.step .len{font-size:20px;font-weight:600;color:#191c22;}
.step .lko{font-size:13.5px;color:#6f757e;margin-top:4px;font-weight:500;}
.step .den{font-size:14.5px;color:#565c66;margin-top:16px;line-height:1.6;}
.step .dko{font-size:13.5px;color:#565b63;margin-top:8px;line-height:1.65;}
.dims{display:flex;gap:22px;margin-top:50px;}
.dimc{flex:1;background:#F7F6F2;border-radius:22px;padding:34px 22px;text-align:center;}
.dimc .dl{font-family:'Inter';font-size:12.5px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#9aa0a8;}
.dimc .dlk{font-size:13px;color:#7c818a;margin-top:3px;}
.dimc .dv{font-size:42px;font-weight:700;color:var(--acc);margin-top:14px;font-family:'Inter';letter-spacing:-.02em;}
"""
CHECK = ('<svg class="sic" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="12" fill="var(--acc)"/>'
         '<path d="M7 12.4l3 3 7-7.2" stroke="#fff" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/></svg>')
XMARK = ('<svg class="sic" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="12" fill="#e4e6ea"/>'
         '<path d="M8.2 8.2l7.6 7.6M15.8 8.2l-7.6 7.6" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/></svg>')


def head(eyebrow, ten, tko, num):
    return (f'<div class="snum">{num:02d}</div><div class="inner">'
            f'<div class="eyebrow">{E(eyebrow)}</div><div class="h-en">{E(ten)}</div><div class="h-ko">{E(tko)}</div>')


def cards(items):
    g = ""
    for i, it in enumerate(items):
        g += (f'<div class="card"><div class="bar"></div><div class="num">{i+1:02d}</div>'
              f'<div class="nen">{E(it.get("name_en"))}</div><div class="nko">{E(it.get("name_ko"))}</div>'
              f'<div class="den">{E(it.get("desc_en"))}</div><div class="dko">{E(it.get("desc_ko"))}</div></div>')
    return f'<div class="grid">{g}</div>'


def _ph(img):
    """섹션 상단 제품 이미지 카드(예시 구성). img=base64 data URI 또는 경로."""
    return f'<div class="phcard"><img src="{img}"/></div>' if img else ''


def s_comparison(d, num, img=None):
    rows = (d.get("comparison") or {}).get("rows", [])[:8]
    tr = ""
    for j, r in enumerate(rows):
        last = " last" if j == len(rows)-1 else ""
        tr += (f'<tr><td class="pt"><b>{E(r.get("point_en"))}</b><span>{E(r.get("point_ko"))}</span></td>'
               f'<td class="ours{last}"><div class="v y">{CHECK}{E(r.get("ours_en"))}</div></td>'
               f'<td><div class="v n">{XMARK}{E(r.get("others_en"))}</div></td></tr>')
    return ('<div class="sec">' + head("Comparison", d.get("title_en"), d.get("title_ko"), num) + _ph(img) +
            '<table class="cmp"><thead><tr><th></th><th class="ours">Ours</th>'
            f'<th class="oth">Others</th></tr></thead><tbody>{tr}</tbody></table></div></div>')


def s_features(d, num, img=None):
    return ('<div class="sec soft">' + head("Key Features", d.get("title_en"), d.get("title_ko"), num) +
            _ph(img) + cards((d.get("features") or [])[:6]) + '</div></div>')


def s_selling(d, num, img=None):
    return ('<div class="sec">' + head("Why This One", d.get("title_en"), d.get("title_ko"), num) +
            _ph(img) + cards(((d.get("selling") or {}).get("points") or [])[:6]) + '</div></div>')


def s_howto(d, num, img=None):
    steps = (d.get("howto") or [])[:4]; st = ""
    for i, s in enumerate(steps):
        st += (f'<div class="step"><div class="sn">{i+1}</div>'
               f'<div class="len">{E(s.get("label_en"))}</div><div class="lko">{E(s.get("label_ko"))}</div>'
               f'<div class="den">{E(s.get("desc_en"))}</div><div class="dko">{E(s.get("desc_ko"))}</div></div>')
    return ('<div class="sec soft">' + head("How To Use", d.get("title_en"), d.get("title_ko"), num) + _ph(img) +
            f'<div class="steps">{st}</div></div></div>')


def s_size(d, num, img=None):
    dims = [x for x in (d.get("dims") or []) if x.get("value")][:4]; dc = ""
    for x in dims:
        dc += (f'<div class="dimc"><div class="dl">{E(x.get("label_en"))}</div>'
               f'<div class="dlk">{E(x.get("label_ko"))}</div><div class="dv">{E(x.get("value"))}</div></div>')
    return ('<div class="sec">' + head("Size Guide", d.get("title_en") or "Dimensions", d.get("title_ko") or "실측 사이즈", num) + _ph(img) +
            f'<div class="dims">{dc}</div></div></div>')


TPL = {"comparison": s_comparison, "features": s_features, "selling": s_selling, "howto": s_howto, "size": s_size}


def vision(img_bytes):
    # ★예시 구성: 모든 섹션을 features형(이미지+카드)으로 통일 → 항상 features 분석.
    #   + shot 분류로 인물/라이프스타일 컷을 임베드에서 배제.
    PR = ('이 제품 이미지를 분석해 한국 프리미엄 쇼핑몰 상세 섹션용 JSON. 영문(en)+한글(ko) 이중. '
          '★효능·기능성·의약품 표현 금지(식품표시광고법). 이 이미지에서 실제로 보이는 객관 사실만.\n'
          '{"shot":"product/lifestyle/marketing 중 하나. 사람·손이 보이거나 사용장면이면 lifestyle. '
          '글자·라벨·여러컷 콜라주·패키지박스가 보이면 marketing. 깨끗한 배경의 제품 단독·세트 컷이면 product.",'
          '"title_en":"섹션 제목(영문, 이 이미지의 핵심 주제 — 예: Product Features / Material & Form)",'
          '"title_ko":"섹션 제목(한글)",'
          '"features":[{"name_en":"","name_ko":"","desc_en":"","desc_ko":""}]}\n'
          'features 3~4개는 ★제품 자체의 특징만(배경·소품·사람 묘사 금지). 각 desc 1~2문장. JSON만.')
    b = base64.b64encode(img_bytes).decode()
    base = f"https://generativelanguage.googleapis.com/v1beta/models/{VIS}:generateContent?key="
    # ★키 로테이션: 503(과부하)이면 다음 키/계정으로 자동 전환 (두 계정 왔다갔다)
    for rnd in range(2):
        for k in _gemini_keys():
            try:
                r = requests.post(base + k, timeout=60, json={"contents": [{"parts": [
                    {"text": PR}, {"inline_data": {"mime_type": "image/jpeg", "data": b}}]}]})
                if r.status_code != 200:
                    continue  # 503 등 → 다음 키
                t = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                o, _ = json.JSONDecoder().raw_decode(t[t.find("{"):].strip())
                o["kind"] = "features"
                o["shot"] = (o.get("shot") or "product").lower().strip()
                return o
            except Exception:
                pass
            time.sleep(0.4)
    return {"kind": "lifestyle", "shot": "lifestyle"}


def clean_imgs(pid, n=5):
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = con.execute("SELECT local_path FROM image_cache WHERE product_id=? AND public_url IS NOT NULL ORDER BY image_idx LIMIT 8", (pid,)).fetchall()
    imgs = [r["local_path"] for r in rows if r["local_path"] and os.path.isfile(r["local_path"])]
    try:
        sys.path.insert(0, str(BASE))
        from backend.purchase.services.image_classifier import clean_local_paths
        cl = [p for p in clean_local_paths(int(pid)) if os.path.isfile(p)]
        if cl: imgs = cl
    except Exception as e:
        print("  img-class skip:", str(e)[:50], flush=True)
    return imgs[:n]


async def render(sections):
    from playwright.async_api import async_playwright
    files = []
    async with async_playwright() as p:
        br = await p.chromium.launch(args=["--no-sandbox"])
        for i, (kind, body) in enumerate(sections):
            doc = (f'<!doctype html><html><head><meta charset="utf-8"><style>:root{{--acc:{ACCENT};}}{CSS}</style>'
                   f'</head><body>{body}</body></html>')
            page = await br.new_page(viewport={"width": 1080, "height": 800}, device_scale_factor=2)
            await page.set_content(doc, wait_until="networkidle"); await page.wait_for_timeout(500)
            el = await page.query_selector(".sec")
            fp = MEDIA / f"edsec_{i}.png"
            await el.screenshot(path=str(fp)); files.append((i, fp, kind)); await page.close()
        await br.close()
    return files


GEN_MODEL = "gemini-2.5-flash-image"
GEN_PROMPT = ("Premium e-commerce product photographer. Use the provided image ONLY as reference for the "
              "product's true appearance (shape, color, material, every detail faithful and unchanged). Generate "
              "a NEW clean studio photo showing ONLY the product item(s) themselves. ★STRICTLY EXCLUDE: any "
              "packaging, boxes, plastic cases/containers, retail labels, price tags, stickers, and ALL text, "
              "letters and numbers. Show only the bare items. Neatly arrange them centered on a clean minimal "
              "soft-gradient background, professional soft lighting, subtle shadow, generous margin, high-end "
              "catalog look. No text, no logos, no watermarks, no people, no hands.")


def _gemini_keys():
    ks = []
    # ★새 계정(GEMINI_API_KEY_5) 우선 → 503이면 구 계정 키들로 폴백 (두 계정 자동 로테이션)
    for n in ("GEMINI_API_KEY_5", "GEMINI_API_KEY", "GEMINI_API_KEY_2",
              "GEMINI_API_KEY_3", "GEMINI_API_KEY_4", "GEMINI_API_KEY_FALLBACK"):
        v = os.environ.get(n)
        if v and v not in ks:
            ks.append(v)
    return ks


def gen_design_cut(ref_path):
    """깨끗한 제품컷이 없을 때(합성/인물뿐) 참조 이미지로 Gemini 디자인컷 생성 → design_cut.jpg 반환.
    503(과부하) 대비 키 로테이션 + 2라운드 재시도."""
    from io import BytesIO
    out = MEDIA / "design_cut.jpg"
    b = base64.b64encode(Path(ref_path).read_bytes()).decode()
    body = {"contents": [{"parts": [{"text": GEN_PROMPT},
            {"inline_data": {"mime_type": "image/jpeg", "data": b}}]}],
            "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]}}
    base = f"https://generativelanguage.googleapis.com/v1beta/models/{GEN_MODEL}:generateContent?key="
    keys = _gemini_keys()
    for rnd in range(2):
        for k in keys:
            try:
                r = requests.post(base + k, json=body, timeout=180)
                if r.status_code == 200:
                    for p in r.json()["candidates"][0]["content"]["parts"]:
                        inl = p.get("inline_data") or p.get("inlineData")
                        if inl and inl.get("data"):
                            im = PI.open(BytesIO(base64.b64decode(inl["data"]))).convert("RGB")
                            if im.width != 1080:
                                im = im.resize((1080, int(im.height * 1080 / im.width)))
                            im.save(out, "JPEG", quality=88)
                            return out
                    print("  design_cut: 응답에 이미지 없음", flush=True)
                else:
                    print(f"  design_cut status {r.status_code} → 키 전환", flush=True)
            except Exception as e:
                print(f"  design_cut 예외 {str(e)[:40]} → 키 전환", flush=True)
            time.sleep(2)
    return None


def _render_manifest(sections):
    """sections → edsec_*.jpg + ed_manifest.json 기록(공통)."""
    files = asyncio.run(render(sections))
    manifest = []
    for i, fp, kind in files:
        im = PI.open(fp).convert("RGB")
        if im.width != 1080:
            im = im.resize((1080, int(im.height * 1080 / im.width)))
        out = MEDIA / f"edsec_{i}.jpg"
        im.save(out, "JPEG", quality=86)
        fp.unlink(missing_ok=True)
        manifest.append(f"/api/pa/images/products/{PID}/edsec_{i}.jpg")
    (MEDIA / "ed_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
    return manifest


def rerender_themed():
    """테마 디자인컷(themed_cut.jpg)으로 에디토리얼 1섹션을 재렌더 → 상세 1번이 테마 연출이 됨.
    design_cut.jpg(깨끗한 제품컷)로 vision(특징) 추출, themed_cut.jpg를 임베드. design_cut은 보존(미삭제)."""
    themed = MEDIA / "themed_cut.jpg"
    if not themed.exists():
        print("themed_cut 없음 → 재렌더 스킵", flush=True); return
    ref = MEDIA / "design_cut.jpg"
    vis_src = ref if ref.exists() else themed
    m = vision(vis_src.read_bytes())
    if not (m.get("features")):
        print("features 0 → 재렌더 스킵", flush=True); return
    img_uri = "data:image/jpeg;base64," + base64.b64encode(themed.read_bytes()).decode()
    sections = [("features", s_features(m, 1, img_uri))]
    manifest = _render_manifest(sections)
    print(f"  ✓ themed 에디토리얼 재렌더 {len(manifest)} blocks (임베드: themed_cut.jpg)", flush=True)


def main():
    MEDIA.mkdir(parents=True, exist_ok=True)
    (MEDIA / "design_cut.jpg").unlink(missing_ok=True)  # 이번 판단 결과만 남기기(stale 제거)
    # ★제품 이미지 1장만(아마존 원본 사진 미사용, 저작권). rep_nuki 우선 탐색,
    #   깨끗한 product 컷이면 임베드. 합성(marketing)·인물(lifestyle)뿐이면 텍스트 카드만(임베드 생략).
    rep = MEDIA / "rep_nuki.jpg"
    # ★vision 호출 캡(비용절감): rep + 깨끗한컷 3장 = 최대 4장만 분석. 깨끗한 제품컷은 보통 앞쪽에 있어 손실 미미.
    order = ([rep] if rep.exists() else []) + [Path(p) for p in clean_imgs(PID, 3)]
    print(f"=== editorial master {PID} | 이미지후보 {len(order)} ===", flush=True)
    prod, any_feat, seen = None, None, set()
    for i, lp in enumerate(order):
        if str(lp) in seen:
            continue
        seen.add(str(lp))
        m = vision(Path(lp).read_bytes())
        feats = m.get("features") or []
        shot = m.get("shot", "product")
        print(f"  [{i}] shot={shot} feat={len(feats)} | {Path(lp).name}", flush=True)
        if not feats:
            continue
        if any_feat is None:
            any_feat = (m, lp)
        if shot == "product":
            prod = (m, lp); break
    if prod:
        m, lp = prod
        img_uri = None if _TEXTONLY else ("data:image/jpeg;base64," + base64.b64encode(Path(lp).read_bytes()).decode())
        sections = [("features", s_features(m, 1, img_uri))]
        print(f"  공유 섹션 1 (제품컷 임베드: {Path(lp).name})", flush=True)
    elif any_feat:
        m, lp = any_feat
        # 깨끗한 제품컷 없음(합성/인물뿐) → Gemini 디자인컷 생성해 임베드(저작권 안전)
        dc = gen_design_cut(lp)
        if dc:
            img_uri = "data:image/jpeg;base64," + base64.b64encode(Path(dc).read_bytes()).decode()
            sections = [("features", s_features(m, 1, img_uri))]
            print(f"  공유 섹션 1 (디자인컷 임베드: {dc.name})", flush=True)
        else:
            sections = [("features", s_features(m, 1, None))]
            print("  공유 섹션 1 (디자인컷 실패 → 텍스트 카드만)", flush=True)
    else:
        print("features 0 → 에디토리얼 생략", flush=True)
        (MEDIA / "ed_manifest.json").write_text("[]"); return
    files = asyncio.run(render(sections))
    # PNG → JPG(1080폭) + manifest URL
    manifest = []
    _pfx = "edsec_shared_" if _TEXTONLY else "edsec_"
    _mf = "ed_shared.json" if _TEXTONLY else "ed_manifest.json"
    for i, fp, kind in files:
        im = PI.open(fp).convert("RGB")
        if im.width != 1080: im = im.resize((1080, int(im.height*1080/im.width)))
        out = MEDIA / f"{_pfx}{i}.jpg"; im.save(out, "JPEG", quality=86)
        fp.unlink(missing_ok=True)
        manifest.append(f"/api/pa/images/products/{PID}/{_pfx}{i}.jpg")
    (MEDIA / _mf).write_text(json.dumps(manifest, ensure_ascii=False))
    print(f"  ✓ {_mf} {len(manifest)} blocks", flush=True)


if len(sys.argv) > 2 and sys.argv[2] == "themed":
    rerender_themed()
else:
    main()
