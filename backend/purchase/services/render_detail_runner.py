# -*- coding: utf-8 -*-
"""프리미엄 상세 섹션 렌더 v2 (HTML/CSS + Playwright). 디자인 업그레이드.
비전이 원본 콘텐츠를 영문+한글 이중 추출 → 에디토리얼 HTML(섹션넘버·SVG아이콘·히어로) → 스크린샷(2x).
웹폰트 CDN: Pretendard(KO)+Inter/Playfair(EN). 제품컷 1회 생성 재사용.
"""
import os, sys, io, json, base64, re, time, html, asyncio, sqlite3, requests
from pathlib import Path
from dotenv import load_dotenv

BASE = Path.home() / "CharisG-Platform/charisg-platform"
load_dotenv(BASE / ".env")
DB = BASE / "backend/purchase/purchase.db"
KEY = os.environ["GEMINI_API_KEY"]
PIDS = sys.argv[1:] or ["4691"]
ROOT = Path("/tmp/render_detail"); ROOT.mkdir(parents=True, exist_ok=True)
OUT = None
USAGE = []
CUR = {"pid": None}
GEN = ["gemini-3-pro-image", "gemini-3.1-flash-image", "gemini-2.5-flash-image"]
VIS = "gemini-2.5-flash-lite"  # 전상품 에디토리얼(2026-07-05)
URL = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key=" + KEY


def vision(img_bytes):
    PR = ('상세 이미지를 분석해 한국 프리미엄 쇼핑몰용 JSON. 원본의 구체 정보를 빠짐없이, '
          '영문(en)+한글(ko) 이중으로. 모든 desc는 반드시 채워라(빈값 금지). '
          '★★효능·기능성·질병·의약품 표현 절대 금지(식품표시광고법): 면역/항산화/디톡스/흡수율/혈액순환/'
          '피부개선·탄력·미백/체지방감소/항노화/특효/완치/부작용없음/세계최고/의사추천/임상 등 일절 쓰지 말 것. '
          '성분명·함량·정수·소재·형태·섭취방법·원산지·사용법 등 객관 사실만. '
          '★kind는 6개 중 정확히 하나(파이프 금지).\n'
          '{"kind":"comparison|selling|howto|features|size|lifestyle",'
          '"title_en":"섹션 영문 제목(간결)","title_ko":"한글 제목",'
          '"comparison":{"rows":[{"point_en":"비교항목","point_ko":"","ours_en":"우리 장점","others_en":"타사"}]},'
          '"selling":{"badge_en":"예 11,000+ Sold","points":[{"name_en":"","name_ko":"","desc_en":"","desc_ko":""}]},'
          '"howto":[{"label_en":"","label_ko":"","desc_en":"","desc_ko":""}],'
          '"features":[{"name_en":"","name_ko":"","desc_en":"","desc_ko":""}],'
          '"dims":[{"label_en":"","label_ko":"","value":"숫자cm"}]}\n'
          '해당 kind 필드만. lifestyle=빈값. JSON만.')
    b = base64.b64encode(img_bytes).decode()
    last = None
    for _ in range(3):
        try:
            r = requests.post(URL.format(m=VIS), timeout=60, json={"contents": [{"parts": [
                {"text": PR}, {"inline_data": {"mime_type": "image/jpeg", "data": b}}]}]})
            j = r.json(); um = j.get("usageMetadata", {})
            USAGE.append({"pid": CUR["pid"], "call": "vision", "prompt": um.get("promptTokenCount", 0),
                          "cand": um.get("candidatesTokenCount", 0)})
            t = j["candidates"][0]["content"]["parts"][0]["text"]
            o, _ = json.JSONDecoder().raw_decode(t[t.find("{"):].strip())
            o["kind"] = (o.get("kind") or "lifestyle").replace(",", "|").split("|")[0].strip()
            # 효능 부당광고 문구 제거 (식품표시광고법) — 모든 텍스트 후처리 정제(안전망)
            try:
                import sys as _s
                if str(BASE) not in _s.path:
                    _s.path.insert(0, str(BASE))
                from backend.purchase.services import clean_policy as _cp

                def _scrub(x):
                    if isinstance(x, str):
                        return _cp.sanitize_efficacy_claims(x)
                    if isinstance(x, list):
                        return [_scrub(v) for v in x]
                    if isinstance(x, dict):
                        return {k: _scrub(v) for k, v in x.items()}
                    return x
                o = _scrub(o)
            except Exception:
                pass
            return o
        except Exception as e:
            last = e; time.sleep(2)
    print("vision err", last); return {"kind": "lifestyle"}


def gen_photo(ref_bytes):
    prompt = ("Premium product photographer. Use the provided image ONLY as reference for the product's true "
              "appearance. Generate a NEW original studio photo of THIS exact product, faithful, clean soft "
              "neutral background, centered, generous margin, soft shadow. No text/logos/watermarks.")
    body = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg",
            "data": base64.b64encode(ref_bytes).decode()}}]}], "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]}}
    for m in GEN:
        try:
            r = requests.post(URL.format(m=m), json=body, timeout=180)
            if r.status_code != 200: continue
            j = r.json(); um = j.get("usageMetadata", {})
            for p in j["candidates"][0]["content"]["parts"]:
                inl = p.get("inline_data") or p.get("inlineData")
                if inl and inl.get("data"):
                    USAGE.append({"pid": CUR["pid"], "call": "gen", "model": m,
                                  "prompt": um.get("promptTokenCount", 0), "cand": um.get("candidatesTokenCount", 0)})
                    return base64.b64decode(inl["data"])
        except Exception: pass
    return None


def E(s): return html.escape(str(s or ""))


CHECK = ('<svg class="sic" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="12" fill="var(--acc)"/>'
         '<path d="M7 12.4l3 3 7-7.2" stroke="#fff" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/></svg>')
XMARK = ('<svg class="sic" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="12" fill="#e4e6ea"/>'
         '<path d="M8.2 8.2l7.6 7.6M15.8 8.2l-7.6 7.6" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/></svg>')

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
.eyebrow{display:flex;align-items:center;gap:14px;font-family:'Inter';text-transform:uppercase;letter-spacing:.24em;font-size:12.5px;font-weight:600;color:var(--acc);}
.eyebrow::before{content:"";width:30px;height:2px;background:var(--acc);display:inline-block;}
.h-en{font-family:'Playfair Display',serif;font-size:52px;font-weight:600;letter-spacing:-.015em;color:#13151a;margin-top:18px;line-height:1.08;}
.h-ko{font-size:18px;color:#6b7079;margin-top:12px;font-weight:500;letter-spacing:.01em;}
.photo{width:100%;border-radius:26px;box-shadow:0 30px 70px rgba(20,28,38,.12);display:block;margin:48px 0 4px;}
/* comparison */
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
.cmp .v.n{color:#b7bcc3;justify-content:flex-start;}
.sic{width:23px;height:23px;flex:0 0 23px;}
/* grid cards */
.grid{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-top:50px;}
.card{position:relative;background:#fff;border:1px solid #eceef0;border-radius:22px;padding:34px 32px 32px;box-shadow:0 10px 30px rgba(20,28,38,.05);}
.card .num{font-family:'Inter';font-weight:700;font-size:14px;color:var(--acc);letter-spacing:.1em;}
.card .nen{font-size:22px;font-weight:600;color:#191c22;margin-top:10px;letter-spacing:-.01em;}
.card .nko{font-size:14px;color:#6f757e;margin-top:4px;font-weight:500;}
.card .den{font-size:15px;color:#565c66;margin-top:18px;line-height:1.62;}
.card .dko{font-size:14px;color:#565b63;margin-top:9px;line-height:1.72;}
.card .bar{position:absolute;top:34px;right:32px;width:34px;height:3px;border-radius:2px;background:var(--acc);opacity:.85;}
/* selling */
.herowrap{position:relative;margin:48px 0 4px;border-radius:26px;overflow:hidden;box-shadow:0 30px 70px rgba(20,28,38,.12);}
.herowrap img{width:100%;display:block;}
.herowrap .scrim{position:absolute;inset:0;background:linear-gradient(180deg,rgba(0,0,0,0) 55%,rgba(0,0,0,.45) 100%);}
.badge{position:absolute;left:34px;bottom:30px;display:inline-flex;align-items:center;gap:9px;background:rgba(255,255,255,.94);color:#1c1f25;font-family:'Inter';font-weight:600;font-size:15px;letter-spacing:.01em;padding:12px 22px;border-radius:30px;backdrop-filter:blur(4px);}
.badge .dot{width:8px;height:8px;border-radius:50%;background:var(--acc);}
/* howto */
.steps{display:flex;gap:24px;margin-top:54px;}
.step{flex:1;background:#fff;border:1px solid #ececec;border-radius:22px;padding:36px 28px 32px;box-shadow:0 10px 30px rgba(20,28,38,.05);position:relative;}
.step .sn{width:58px;height:58px;border-radius:16px;background:var(--acc);color:#fff;font-family:'Inter';font-weight:700;font-size:24px;display:flex;align-items:center;justify-content:center;margin-bottom:22px;}
.step .len{font-size:20px;font-weight:600;color:#191c22;letter-spacing:-.01em;}
.step .lko{font-size:13.5px;color:#6f757e;margin-top:4px;font-weight:500;}
.step .den{font-size:14.5px;color:#565c66;margin-top:16px;line-height:1.6;}
.step .dko{font-size:13.5px;color:#565b63;margin-top:8px;line-height:1.65;}
.step .arrow{position:absolute;top:54px;right:-16px;color:#d6d3cc;font-size:22px;z-index:2;}
/* size */
.dims{display:flex;gap:22px;margin-top:50px;}
.dimc{flex:1;background:#F7F6F2;border-radius:22px;padding:34px 22px;text-align:center;}
.dimc .dl{font-family:'Inter';font-size:12.5px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#9aa0a8;}
.dimc .dlk{font-size:13px;color:#7c818a;margin-top:3px;}
.dimc .dv{font-size:42px;font-weight:700;color:var(--acc);margin-top:14px;font-family:'Inter';letter-spacing:-.02em;}
"""


def head(eyebrow, ten, tko, num):
    return (f'<div class="snum">{num:02d}</div><div class="inner">'
            f'<div class="eyebrow">{E(eyebrow)}</div><div class="h-en">{E(ten)}</div>'
            f'<div class="h-ko">{E(tko)}</div>')


def s_comparison(d, photo, num):
    rows = (d.get("comparison") or {}).get("rows", [])[:8]
    tr = ""
    for j, r in enumerate(rows):
        last = " last" if j == len(rows)-1 else ""
        tr += (f'<tr><td class="pt"><b>{E(r.get("point_en"))}</b><span>{E(r.get("point_ko"))}</span></td>'
               f'<td class="ours{last}"><div class="v y">{CHECK}{E(r.get("ours_en"))}</div></td>'
               f'<td><div class="v n">{XMARK}{E(r.get("others_en"))}</div></td></tr>')
    return ('<div class="sec">' + head("Comparison", d.get("title_en"), d.get("title_ko"), num) +
            '<table class="cmp"><thead><tr><th></th><th class="ours">Ours</th>'
            f'<th class="oth">Others</th></tr></thead><tbody>{tr}</tbody></table></div></div>')


def cards(items):
    g = ""
    for i, it in enumerate(items):
        g += (f'<div class="card"><div class="bar"></div><div class="num">{i+1:02d}</div>'
              f'<div class="nen">{E(it.get("name_en"))}</div><div class="nko">{E(it.get("name_ko"))}</div>'
              f'<div class="den">{E(it.get("desc_en"))}</div><div class="dko">{E(it.get("desc_ko"))}</div></div>')
    return f'<div class="grid">{g}</div>'


def s_features(d, photo, num):
    return ('<div class="sec soft">' + head("Key Features", d.get("title_en"), d.get("title_ko"), num) +
            (f'<img class="photo" src="{photo}">' if photo else '') +
            cards((d.get("features") or [])[:6]) + '</div></div>')


def s_selling(d, photo, num):
    s = d.get("selling") or {}
    hero = ""
    if photo:
        badge = (f'<div class="badge"><span class="dot"></span>{E(s.get("badge_en"))}</div>' if s.get("badge_en") else "")
        hero = f'<div class="herowrap"><img src="{photo}"><div class="scrim"></div>{badge}</div>'
    return ('<div class="sec">' + head("Why This One", d.get("title_en"), d.get("title_ko"), num) +
            hero + cards((s.get("points") or [])[:6]) + '</div></div>')


def s_howto(d, photo, num):
    steps = (d.get("howto") or [])[:4]
    st = ""
    for i, s in enumerate(steps):
        arrow = '<div class="arrow">›</div>' if i < len(steps)-1 else ''
        st += (f'<div class="step"><div class="sn">{i+1}</div>'
               f'<div class="len">{E(s.get("label_en"))}</div><div class="lko">{E(s.get("label_ko"))}</div>'
               f'<div class="den">{E(s.get("desc_en"))}</div><div class="dko">{E(s.get("desc_ko"))}</div>{arrow}</div>')
    return ('<div class="sec soft">' + head("How To Use", d.get("title_en"), d.get("title_ko"), num) +
            f'<div class="steps">{st}</div></div></div>')


def s_size(d, photo, num):
    dims = [x for x in (d.get("dims") or []) if x.get("value")][:4]
    dc = ""
    for x in dims:
        dc += (f'<div class="dimc"><div class="dl">{E(x.get("label_en"))}</div>'
               f'<div class="dlk">{E(x.get("label_ko"))}</div><div class="dv">{E(x.get("value"))}</div></div>')
    return ('<div class="sec">' + head("Size Guide", d.get("title_en") or "Dimensions", d.get("title_ko") or "실측 사이즈", num) +
            (f'<img class="photo" src="{photo}">' if photo else '') +
            f'<div class="dims">{dc}</div></div></div>')


TPL = {"comparison": s_comparison, "features": s_features, "selling": s_selling, "howto": s_howto, "size": s_size}


async def render(sections, accent):
    from playwright.async_api import async_playwright
    files = []
    async with async_playwright() as p:
        br = await p.chromium.launch(args=["--no-sandbox"])
        for i, (kind, body) in enumerate(sections):
            doc = (f'<!doctype html><html><head><meta charset="utf-8"><style>:root{{--acc:{accent};}}{CSS}</style>'
                   f'</head><body>{body}</body></html>')
            page = await br.new_page(viewport={"width": 1080, "height": 800}, device_scale_factor=2)
            await page.set_content(doc, wait_until="networkidle")
            await page.wait_for_timeout(500)
            el = await page.query_selector(".sec")
            fp = OUT / f"{i}_{kind}.png"
            await el.screenshot(path=str(fp)); files.append(fp); await page.close()
        await br.close()
    return files


def process(pid):
    global OUT
    OUT = ROOT / str(pid); OUT.mkdir(parents=True, exist_ok=True); CUR["pid"] = pid
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = con.execute("SELECT local_path FROM image_cache WHERE product_id=? AND public_url IS NOT NULL "
                       "ORDER BY image_idx LIMIT 5", (pid,)).fetchall()
    imgs = [r["local_path"] for r in rows if r["local_path"] and os.path.isfile(r["local_path"])]
    # ★저작권 게이트 — 마케팅 그래픽(오버레이 문구·비교표·콜아웃) 제외, 클린 제품컷만 rep+섹션에 사용.
    #   classify_images 캐시는 build_payload 의 갤러리 필터와 공유됨.
    try:
        if str(BASE) not in sys.path:
            sys.path.insert(0, str(BASE))
        from backend.purchase.services.image_classifier import clean_local_paths
        _clean = [p for p in clean_local_paths(pid) if os.path.isfile(p)]
        if _clean:
            n_excl = len(imgs) - len([p for p in imgs if p in set(_clean)])
            imgs = _clean[:5]
            print(f"  [저작권게이트] 마케팅 제외 후 클린 {len(imgs)}장", flush=True)
    except Exception as _e:
        print("  img-class skip:", str(_e)[:70], flush=True)
    p = con.execute("SELECT title_ko FROM products WHERE id=?", (pid,)).fetchone()
    print(f"\n=== pid={pid} {(p['title_ko'] or '')[:28]} ({len(imgs)}장) ===", flush=True)
    if not imgs: return
    from PIL import Image as _PI
    # 제품컷 = 실제 제품 이미지 (AI 생성 없음 — 거의 무료)
    ri = _PI.open(imgs[0]).convert("RGB"); ri.thumbnail((1000, 1000))
    rc = _PI.new("RGB", (1080, 1080), (255, 255, 255)); rc.paste(ri, ((1080-ri.width)//2, (1080-ri.height)//2))
    rc.save(OUT / "_photo.jpg", "JPEG", quality=92)
    photo = "data:image/jpeg;base64," + base64.b64encode((OUT / "_photo.jpg").read_bytes()).decode()
    accent = "#1f6e43"
    # 비전 분류 → 템플릿 가능 후보 수집
    cands = []
    for i, lp in enumerate(imgs):
        m = vision(Path(lp).read_bytes()); kind = m.get("kind", "lifestyle")
        tc = (m.get("theme_color") or "").strip()
        if re.match(r"^#[0-9a-fA-F]{6}$", tc): accent = tc
        print(f"  [{i}] {kind} | {m.get('title_en','')}", flush=True)
        if kind in TPL:
            cands.append((kind, m, lp))
    # 다양한 kind 우선 최대 3개 (부족하면 채움)
    picked, seen = [], set()
    for c in cands:
        if c[0] not in seen:
            picked.append(c); seen.add(c[0])
        if len(picked) >= 3: break
    for c in cands:
        if len(picked) >= 3: break
        if c not in picked: picked.append(c)
    # 섹션별 서로 다른 실제 이미지 사용 (이미지 다양화)
    sections = []
    for n, (kind, m, lp) in enumerate(picked[:3]):
        si = _PI.open(lp).convert("RGB"); si.thumbnail((1000, 1000))
        ci = _PI.new("RGB", (1080, 1080), (255, 255, 255)); ci.paste(si, ((1080-si.width)//2, (1080-si.height)//2))
        ci.save(OUT / f"_p{n}.jpg", "JPEG", quality=92)
        sp = "data:image/jpeg;base64," + base64.b64encode((OUT / f"_p{n}.jpg").read_bytes()).decode()
        sections.append((kind, TPL[kind](m, sp, n + 1)))
    print(f"  섹션 {len(sections)}개: {[s[0] for s in sections]}", flush=True)
    files = asyncio.run(render(sections, accent))
    # ── 전체 페이지 조립 (브랜드배너 + 실물 대표컷 + HTML섹션 + 스펙 + 안내배너) ──
    from PIL import Image as PI
    BAN = BASE / "backend/purchase/media/banners"
    def _op(p): return PI.open(p).convert("RGB") if Path(p).exists() else None
    def _fit(im, w=1080): return im if im.width == w else im.resize((w, int(im.height*w/im.width)))
    spec = None
    try:
        sys.path.insert(0, str(BASE))
        from backend.purchase.services.spec_table import render_spec_table
        su = render_spec_table(int(pid))
        if su and "/api/pa/images/" in su:
            sp = BASE / "backend/purchase/media" / su.split("/api/pa/images/")[-1]
            if sp.exists(): spec = PI.open(sp).convert("RGB")
    except Exception as e:
        print("  spec:", str(e)[:60])
    rep = PI.open(OUT / "_photo.jpg").convert("RGB")
    parts = [_op(BAN/"banner_1_brand.jpg"), rep] + [PI.open(f).convert("RGB") for f in files] + \
            [spec, _op(BAN/"banner_2_shipping.jpg"), _op(BAN/"banner_3_amazon.jpg"), _op(BAN/"banner_4_purchase_notice.jpg")]
    scc = [_fit(x) for x in parts if x is not None]
    Ht = sum(x.height for x in scc); page = PI.new("RGB", (1080, Ht), (255, 255, 255)); y = 0
    for x in scc:
        page.paste(x, (0, y)); y += x.height
    page.save(OUT / "full_page.jpg", quality=88)
    print(f"  full_page 1080x{Ht} ({len(scc)} blocks, 섹션 {len(files)}) — AI생성 0", flush=True)


for _pid in PIDS:
    try:
        process(_pid)
    except Exception as e:
        print(f"  pid={_pid} ERROR {type(e).__name__}: {e}", flush=True)
Path(ROOT / "_usage.json").write_text(json.dumps(USAGE, ensure_ascii=False))
print(f"\n완료. usage {len(USAGE)}콜 → {ROOT}/_usage.json")
