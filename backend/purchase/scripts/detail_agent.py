# -*- coding: utf-8 -*-
"""detail_agent.py — 상세페이지 생성 에이전트 v2 (2026-08-08)

v1 대비 변경
  · 섹션 템플릿 전면 재설계. v1 은 모든 섹션이 '제목 + 카드 2열'이라 리듬이 없었다.
    섹션 타입마다 레이아웃 골격을 다르게 준다(풀블리드/지그재그/칩/타임라인/대비표).
  · 이미지 풀 관리 — 섹션마다 다른 컷을 배정한다(v1 은 hero·features 가 같은 사진).
  · 금지 표현 후처리 필터(최상급·효능) — 프롬프트만으로는 새어나갔다.

★설계 제약 (중요)
  · 최종 산출이 Playwright 스크린샷이라 **애니메이션·호버·스크롤 효과는 전부 무의미**하다.
    첫 프레임만 남는다. 리듬은 구성·타이포·색면 대비로만 만든다.
  · 원본 사진 위에 글자를 얹지 않는다. 저작권상 원본 개변을 피하는 오늘 방침에 따라
    사진은 그대로 두고 인접 색면에 텍스트를 둔다(한글 주석과 같은 원리).
  · 모바일(쿠팡 앱)에서 보므로 1080px 기준 본문 18px 이상, 헤드라인은 크게.

사용: python detail_agent.py <product_id> [--policy quote|none]
산출: /tmp/detail_agent/{pid}/full_page.jpg + plan.json
"""
import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path

import requests

BASE = Path("/home/ubuntu/CharisG-Platform/charisg-platform")
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv
load_dotenv(BASE / ".env", override=True)
# ★분류를 기본으로 켠다 (2026-08-15). 종전엔 "1"(스킵)이 기본이라 저작권 분류가
#   통째로 꺼져 있었고, 전 이미지가 photo 로 폴백돼 브랜드 마케팅컷·모델컷이
#   무검열로 상세에 들어갔다. 끄려면 PA_SKIP_GEMINI=1 을 명시할 것.
os.environ.setdefault("PA_SKIP_GEMINI", "0")

# AI_LEDGER 가 설정돼 있으면 호출을 원장에 기록(비용 측정용). 서브프로세스로 돌 때도 잡힌다.
if os.environ.get("AI_LEDGER"):
    try:
        sys.path.insert(0, str(BASE))
        import ai_ledger as _led
        _led.install()
    except Exception as _e:
        print("[detail_agent] ledger 설치 실패:", _e)

from backend.purchase.database import get_db

OUT_ROOT = Path("/tmp/detail_agent")
W_ASM = 1080
MODEL = "gemini-2.5-flash-lite"

# ── 팔레트 ─────────────────────────────────────────────
INK = "#14213D"        # 딥 네이비 — Charis G 네이비를 더 눌러 대비를 키움
ACCENT = "#E8582D"     # 코랄레드 — 고지배너의 #E8845A 보다 채도를 올려 상품 섹션용
CREAM = "#F4F1EA"
LINE = "#DCD5C8"



# ── 폰트 배율 (2026-08-13) ────────────────────────────────────────────────
# 11번가 가이드가 "상세 이미지 나눔고딕 28px 이상"을 권장한다. 780px 캔버스 기준이라
# 우리 1080px 로 환산하면 약 39px 이다. 현재 본문이 20px 이라 절반에 못 미친다.
# ★근거가 모바일 가독성이라 쿠팡·네이버에도 그대로 유효하다 — 전 채널 공통으로 올린다.
#
# ★11번가 권장 28px 은 **본문 최소선**이다. 헤드라인을 키우라는 게 아니다.
#   균일 배율(1.9)을 썼더니 hero h1 이 86→163px 이 되어 제목이 사진을 덮고 잘렸다.
#   → 작은 것만 끌어올리고 큰 것은 그대로 둔다.
FONT_MIN = int(os.environ.get("DETAIL_FONT_MIN", "39") or 39)    # 0 이면 비활성
FONT_KEEP = 48                                                    # 이 이상은 불변


def _fs(v: float) -> int:
    """폰트 한 값을 보정한다. 단조 증가라 위계가 역전되지 않는다.

        17→39  20→40  25→41  30→43  36→45  48→48  86→86
    """
    if not FONT_MIN or v >= FONT_KEEP:
        return int(round(v))
    return int(max(round(v), round(FONT_MIN + (v - 17) * 0.3)))


def _scale_fonts(html: str) -> str:
    """완성 HTML 의 font-size 를 일괄 배율한다.

    ★구간별 배율이 아니라 **연속 함수**를 쓴다 — 구간을 나누면 경계에서 위계가 역전된다.
      (~20px ×1.9, 21~30px ×1.65 로 하면 20→38 인데 21→35 가 된다)
    ★detail_palette.recolor 와 같은 방식(완성 HTML 후처리)이라 조립 로직을 안 건드린다.
    """
    if not FONT_MIN:
        return html
    return re.sub(r"font-size:\s*(\d+(?:\.\d+)?)px",
                  lambda m: "font-size:%dpx" % _fs(float(m.group(1))), html)

def _keys():
    out = []
    for n in ("GEMINI_API_KEY", "GEMINI_API_KEY_FALLBACK", "GEMINI_API_KEY_2", "GOOGLE_API_KEY"):
        v = os.environ.get(n)
        if v and v not in out:
            out.append(v)
    return out


USAGE = []


def ask(prompt: str, model: str = MODEL) -> dict:
    for i, key in enumerate(_keys(), 1):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        try:
            r = requests.post(url, timeout=120, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json",
                                     "thinkingConfig": {"thinkingBudget": 0}},
            })
        except Exception:
            continue
        if r.status_code == 200:
            j = r.json()
            um = j.get("usageMetadata", {})
            USAGE.append((um.get("promptTokenCount", 0),
                          um.get("candidatesTokenCount", 0) + um.get("thoughtsTokenCount", 0)))
            t = j["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(t[t.find("{"):t.rfind("}") + 1])
        print(f"  키{i}: HTTP {r.status_code}", flush=True)
    raise RuntimeError("모든 Gemini 키 실패")


# ── 재료 수집 ─────────────────────────────────────────
_ATTR_SKIP = {"list_price", "item_name", "bullet_point", "product_description",
              "unspsc_code", "skip_offer", "package_level", "variation_theme",
              "supplier_declared_dg_hz_regulation", "product_site_launch_date",
              "supplier_declared_has_product_identifier_exemption", "batteries_required",
              "batteries_included", "externally_assigned_product_identifier"}


def gather(pid: int) -> dict:
    with get_db() as c:
        p = c.execute("SELECT asin, title_ko, title_en, brand, category_path, "
                      "sp_raw_json, sp_api_facts_json FROM products WHERE id=?", (pid,)).fetchone()
        imgs = c.execute("SELECT image_idx, local_path, public_url FROM image_cache "
                         "WHERE product_id=? AND public_url IS NOT NULL ORDER BY image_idx",
                         (pid,)).fetchall()
    if not p:
        raise SystemExit(f"product {pid} 없음")
    raw = json.loads(p["sp_raw_json"] or "{}")
    at = raw.get("attributes") or {}
    facts = json.loads(p["sp_api_facts_json"] or "{}")

    def a0(k):
        v = at.get(k)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v[0].get("value")
        return v if isinstance(v, (str, int, float, bool)) else None

    bullets = [x.get("value") for x in (at.get("bullet_point") or [])
               if isinstance(x, dict) and x.get("value")] or (facts.get("bullet_points") or [])
    attrs = {}
    for k in at:
        if k in _ATTR_SKIP:
            continue
        v = a0(k)
        if v is None or not str(v).strip() or str(v).lower() in ("unknown", "not_applicable"):
            continue
        attrs[k] = str(v)[:80]

    cls = {}
    try:
        from backend.purchase.services.image_classifier import classify_images
        cls = classify_images(pid) or {}
    except Exception as _e:      # noqa: BLE001
        # ★조용히 넘어가면 안 된다 — 분류 없이 만든 상세는 브랜드 마케팅컷이
        #   무검열로 들어간 상세다. 실행 중 '키1: HTTP 429' 를 실제로 봤다.
        print("   ★저작권 분류 실패 — 전 이미지가 등급 미상이 된다: %s" % str(_e)[:70])
    if not cls:
        print("   ★저작권 분류 결과가 비었다 — PA_SKIP_GEMINI 또는 GEMINI 키를 확인할 것")

    # ★중복 컷 제거 — 아마존 원본은 같은 사진을 2벌씩 갖고 있는 경우가 흔하다(실측 11장 중 5쌍)
    from PIL import Image as _I

    def ah(fp):
        g = _I.open(fp).convert("L").resize((16, 16))
        px = list(g.getdata()); av = sum(px) / len(px)
        return [1 if v > av else 0 for v in px]

    images, seen = [], []
    for r in imgs:
        lp = r["local_path"]
        if not lp or not os.path.isfile(lp):
            continue
        try:
            h = ah(lp)
            if any(sum(a != b for a, b in zip(h, o)) <= 10 for o in seen):
                continue
            seen.append(h)
        except Exception:
            pass
        images.append({"idx": r["image_idx"], "path": lp, "kind": cls.get(lp, "unknown")})

    return {"pid": pid, "brand": p["brand"] or "", "title_ko": p["title_ko"] or "",
            "title_en": p["title_en"] or "", "category": p["category_path"] or "",
            "bullets": bullets[:6], "attrs": attrs, "images": images,
            "description_en": (facts.get("description_en") or "")[:1500]}


# ── 이미지 주제 태거 ──────────────────────────────────
# 기존 image_classifier 는 '표현 형식'(photo/marketing/lifestyle)만 판정한다.
# 어떤 섹션에 어떤 컷을 넣을지 정하려면 '내용 주제'라는 다른 축이 필요하다.
# (실측 사례: howto 섹션에 호환차종표가 배정되고, 정작 EASY INSTALLATION 컷은 안 쓰임)
# ★라이브 갤러리 필터가 쓰는 image_classifier 는 건드리지 않는다 — 캐시 무효화 위험.
SUBJECTS = {
    "product": "제품 단독 사진(배경 단순, 제품만)",
    "install": "설치·조립·장착 방법 안내",
    "compat":  "호환 기종·차종 목록/표",
    "parts":   "구성품·패키지 내용물",
    "usage":   "실제 사용 장면·연출컷",
    "spec":    "치수·구조·스펙 도해",
    "detail":  "부분 확대·소재·마감",
    "compare": "비교(전후/타사)",
    "other":   "그 외",
}

_SUBJ_PROMPT = """상품 이미지 {n}장을 순서대로 준다. 각 이미지가 '무엇에 관한 것인지' 분류하라.

분류값:
{kinds}

판별 규칙(중요):
- 한 장에 여러 패널이 합쳐진 '마케팅 합성컷'은 그 안에 제품 클로즈업이 섞여 있어도
  detail/product 로 보지 마라. 아래에 해당하면 compare 로 분류한다.
  · 우리 제품과 타사 제품을 나란히 놓고 우열을 보이는 것
  · OURS / others / VS / BEFORE / AFTER 같은 대조 문구가 있는 것
  · O·X, 체크·엑스 표시로 좋고 나쁨을 가르는 것
- 제품 위에 설명 문구(영문 카피, 말풍선, 화살표 라벨)가 크게 얹힌 홍보용 합성컷도
  detail/product 가 아니다. 내용에 맞춰 install/spec/compat 중에서 고르고,
  어디에도 안 맞으면 other 로 둔다.
- product 는 '배경이 단순하고 제품만 있으며 글자가 거의 없는' 사진에만 쓴다.
- usage 는 실제 사용 장면 사진에만 쓴다. 합성·도해에는 쓰지 않는다.

출력: {{"tags":["install","compat",...]}}  — 입력 순서 그대로 {n}개. JSON 만."""


def subject_tags(pid: int, images: list) -> list:
    """이미지별 주제 태그. 캐시: media/products/{pid}/img_subject.json"""
    cache = BASE / "backend/purchase/media/products" / str(pid) / "img_subject.json"
    if cache.exists():
        try:
            d = json.loads(cache.read_text())
            if len(d) == len(images):
                return d
        except Exception:
            pass
    if not images:
        return []
    kinds = "\n".join(f"- {k}: {v}" for k, v in SUBJECTS.items())
    parts = [{"text": _SUBJ_PROMPT.format(n=len(images), kinds=kinds)}]
    for im in images:
        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                      "data": base64.b64encode(Path(im["path"]).read_bytes()).decode()}})
    for i, key in enumerate(_keys(), 1):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={key}"
        try:
            r = requests.post(url, timeout=120, json={
                "contents": [{"parts": parts}],
                "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                     "thinkingConfig": {"thinkingBudget": 0}}})
        except Exception:
            continue
        if r.status_code == 200:
            j = r.json(); um = j.get("usageMetadata", {})
            USAGE.append((um.get("promptTokenCount", 0),
                          um.get("candidatesTokenCount", 0) + um.get("thoughtsTokenCount", 0)))
            t = j["candidates"][0]["content"]["parts"][0]["text"]
            tags = json.loads(t[t.find("{"):t.rfind("}") + 1]).get("tags", [])
            tags = [(x if x in SUBJECTS else "other") for x in tags][:len(images)]
            tags += ["other"] * (len(images) - len(tags))
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(tags, ensure_ascii=False))
            except Exception:
                pass
            return tags
        print(f"  주제태거 키{i}: HTTP {r.status_code}", flush=True)
    return ["other"] * len(images)



def _char_limits() -> dict:
    """글자수 한도. 폰트를 키우면 한 줄에 들어가는 글자가 준다 — 배율로 나눈다.

    ★배율 1.0 이면 기존값(10/30/18/15) 그대로다. 공용 파일이라 회귀를 막는다.
    ★name 은 나누지 않는다 — 원래 짧아서(‘쉬운 설치’) 1.9배에도 한 줄에 든다.
      나누면 2~3자가 되어 말이 안 된다.
    """
    # ★계층마다 배율이 다르다 — 그 계층의 실제 폰트에서 역산한다.
    #   본문(20px)은 2배가 되지만 헤드라인(86px)은 그대로라 title 은 원래 길이를 쓴다.
    body_k = _fs(20) / 20.0      # .lead · .zz-desc
    name_k = _fs(32) / 32.0      # .zz-name (항목명)
    h1_k = _fs(86) / 86.0        # .hero h1 — 배율 1.0 이라 원래 길이를 쓴다
    sub_k = _fs(36) / 36.0       # .hero-sub
    return {
        "lim_name": max(6, round(10 / name_k)),
        "lim_desc": max(12, round(30 / body_k)),
        "lim_htitle": max(8, round(18 / h1_k)),
        "lim_hsub": max(7, round(15 / sub_k)),
    }

# ── 에이전트 1콜 ──────────────────────────────────────
SECTION_TYPES = """
hero            : 상품 한 줄 소개. 항상 1개, 맨 앞.
features        : 핵심 특징 3~5개. bullet_point 가 주재료.
compatibility   : 호환 정보(차종·기기). 관련 속성이 있을 때만.
howto           : 설치·사용 단계. 관련 서술이 있을 때만. 각 단계는 무엇을 하는지 구체적으로.
spec_highlight  : 수치 스펙(소재·크기·용량 등). 속성이 풍부할 때.
safety          : 안전·사용연령 주의. 어린이/완구/식품/전기 제품일 때.
care            : 관리·세척·보관. 관련 서술이 있을 때.
"""

PROMPT = """너는 한국 이커머스 상세페이지 기획자다. 아래 해외 상품 자료로
한국 고객용 상세페이지의 섹션 구성과 카피를 만들어라.

## 섹션 타입
{types}

## 규칙
- 섹션 3~5개. hero 는 반드시 포함하고 맨 앞.
- 자료에 근거 없는 섹션·사실은 만들지 마라.
- ★절대 금지: 효능·효과·의학적 주장, 최상급 표현(최고/1위/완벽/최상/가장), 경쟁사 비방.
  "완벽하게 호환" 같은 표현도 금지. "호환됩니다" 로 쓴다.
- 한국 쇼핑몰 말투. 짧고 담백하게. 과장 금지.
- 브랜드·모델명 등 고유명사는 원문 유지.
- ★howto 의 각 단계 name 은 "1단계" 같은 빈 말 금지. 무엇을 하는지 6자 내외로 쓴다.
  (예: "기존 등 제거", "커넥터 연결", "홈에 끼우기")
- items 의 name 은 {lim_name}자 이내, desc 는 {lim_desc}자 이내.
- hero.title 은 {lim_htitle}자 이내로 강하게. hero.subtitle 은 {lim_hsub}자 이내.

## 출력 JSON
{{
  "summary_ko": "이 상품이 무엇인지 한 문장",
  "sections": [
    {{"type":"hero","title":"","subtitle":"","body":""}},
    {{"type":"features","title":"","items":[{{"name":"","desc":""}}]}},
    {{"type":"compatibility","title":"","body":"","items":[{{"name":"","desc":""}}]}},
    {{"type":"howto","title":"","items":[{{"name":"","desc":""}}]}},
    {{"type":"spec_highlight","title":"","items":[{{"name":"","desc":""}}]}},
    {{"type":"safety","title":"","items":[{{"name":"","desc":""}}]}},
    {{"type":"care","title":"","items":[{{"name":"","desc":""}}]}}
  ]
}}
해당 타입에 필요한 필드만. JSON 만 출력.

## 상품 자료
상품명(한글): {title_ko}
상품명(원문): {title_en}
브랜드: {brand}

핵심 설명(아마존 bullet point):
{bullets}

속성:
{attrs}

원문 설명(참고):
{desc}
"""


def plan_and_write(m: dict) -> dict:
    bl = "\n".join(f"- {b}" for b in m["bullets"]) or "(없음)"
    at = "\n".join(f"- {k}: {v}" for k, v in list(m["attrs"].items())[:30]) or "(없음)"
    return ask(PROMPT.format(**_char_limits(),
                             types=SECTION_TYPES, title_ko=m["title_ko"], title_en=m["title_en"],
                             brand=m["brand"], bullets=bl, attrs=at,
                             desc=m["description_en"] or "(없음)"))


# ── 금지표현 후처리 (프롬프트만으로는 샌다) ─────────────
_BAN = [
    (re.compile(r"완벽(하게|한)?\s*"), ""), (re.compile(r"최고의?\s*"), ""),
    (re.compile(r"최상의?\s*"), ""), (re.compile(r"업계\s*1위\s*"), ""),
    (re.compile(r"가장\s+(뛰어난|우수한|좋은)\s*"), ""), (re.compile(r"100%\s*보장"), ""),
]


def scrub(o):
    if isinstance(o, str):
        s = o
        for rx, rep in _BAN:
            s = rx.sub(rep, s)
        return re.sub(r"\s{2,}", " ", s).strip()
    if isinstance(o, list):
        return [scrub(x) for x in o]
    if isinstance(o, dict):
        return {k: scrub(v) for k, v in o.items()}
    return o


# ── 이미지 풀 (섹션마다 다른 컷) ───────────────────────
# 섹션 타입 → 선호 주제(앞일수록 우선). 맞는 게 없으면 나머지에서 아무거나.
SECTION_WANT = {
    "hero":           ("usage", "product", "detail"),
    "features":       ("product", "detail", "usage"),
    "compatibility":  ("compat",),
    "howto":          ("install", "parts"),
    # ★2026-08-15 install·other 추가. M15 가 만든 3번째 컷(실사 3단계 도식)을
    #   자체 태거가 'other' 로 판정해 어느 섹션도 못 받고 버려졌다(29원 낭비).
    #   image_cache 에는 M15 생성본 3장만 있어 other 를 받아도 엉뚱한 컷이 안 온다.
    "spec_highlight": ("spec", "detail", "install", "other"),
    "safety":         (),
    "care":           ("usage", "detail"),
}


class ImagePool:
    """섹션 주제에 맞는 컷을 배정한다. 한 번 쓴 컷은 다시 주지 않는다.
    ★종전엔 형식 우선순위(photo>lifestyle>marketing)로만 꺼내서, 설치방법 섹션에
      호환차종표가 들어가고 정작 설치 안내 컷은 안 쓰이는 일이 생겼다."""

    def __init__(self, images, policy):
        self.policy = policy
        self.pool = list(images)
        self.used = set()

    def _free(self):
        return [im for im in self.pool if im["path"] not in self.used]

    # ★저작권 등급 — 낮을수록 안전하다. marketing 은 브랜드가 만든 크리에이티브라
    #   쿠팡 IP 신고의 주 대상이다(실측: 브랜드 원본 14장 중 photo 는 2장뿐인 경우도 있다).
    GRADE = {"photo": 0, "lifestyle": 1, "marketing": 2}

    def take(self, section_type=None):
        if self.policy == "none":
            return None
        free = self._free()
        if not free:
            return None

        # ★등급을 주제보다 **먼저** 본다 (2026-08-15).
        #   종전엔 주제를 먼저 맞추고 등급은 폴백이라, 분류를 켜도 배치가 안 바뀌었다 —
        #   marketing 컷이라도 주제만 맞으면 그대로 들어갔다.
        #   막지는 않는다. 안전한 등급을 다 쓴 뒤에만 다음 등급으로 내려간다.
        for grade in (0, 1, 2):
            band = [im for im in free if self.GRADE.get(im.get("kind"), 9) == grade]
            if not band:
                continue
            for want in SECTION_WANT.get(section_type, ()):
                for im in band:
                    if im.get("subject") == want:
                        self.used.add(im["path"])
                        self._warn(im, section_type)
                        return im
            # 이 등급 안에 맞는 주제가 없다 → 다음 등급으로
        if section_type in ("compatibility", "howto", "spec_highlight"):
            return None          # ★맞는 컷이 없으면 아예 넣지 않는다(엉뚱한 사진 방지)
        # 주제가 안 맞아도 자리를 채워야 하는 섹션 — 등급 낮은 것부터
        im = sorted(free, key=lambda x: (self.GRADE.get(x.get("kind"), 9), x["idx"]))[0]
        self.used.add(im["path"])
        self._warn(im, section_type)
        return im

    def _warn(self, im, section_type):
        """위험한 컷을 쓸 때는 화면에 남긴다 — 조용히 쓰면 아무도 모른다.

        ★unknown 은 안전한 게 아니라 **모르는 것**이다. 분류가 실패하면 전부 unknown 이
          되는데, 그때 경고가 없으면 사람은 '분류가 됐겠지' 하고 넘어간다.
        """
        k = im.get("kind")
        if k == "marketing":
            print("   [주의] %s 섹션에 marketing 컷 사용 — 저작권 위험 (%s)"
                  % (section_type or "?", Path(im["path"]).name))
        elif k in (None, "", "unknown"):
            print("   [주의] %s 섹션에 **등급 미상** 컷 사용 — 분류가 안 됐다 (%s)"
                  % (section_type or "?", Path(im["path"]).name))


def data_uri(im):
    if not im:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(Path(im["path"]).read_bytes()).decode()


def E(s):
    import html as _h
    return _h.escape(str(s or ""))


# ── 섹션 렌더러 — 타입마다 골격을 다르게 ────────────────
def sec_hero(s, pool):
    """사진을 위에 두고 카피를 그 **아래** 밴드에 둔다.

    ★오버레이(사진 위에 글자)를 2026-08-13 에 없앴다 — 두 가지가 겹쳤다:
        ① `detail-page-agent` 원칙 "원본 사진 위에 글자를 얹지 않는다" 위반
        ② 폰트를 키우자 제목이 제품을 덮고 잘렸다("차량 루프랙 패 / 드")
      스크림을 하단 52% 에만 깔아도 제목 자체가 길어지면 소용이 없다.
    ★사진이 위, 텍스트가 아래다 — 종전 밴드는 반대였다.
    """
    _im = pool.take("hero")
    u = data_uri(_im)
    photo = (f'<div class="hero-photo"><img src="{u}"></div>') if u else ""
    return f"""<section class="hero">
  {photo}
  <div class="hero-band">
    <div class="eyebrow">Product</div>
    <h1>{E(s.get('title'))}</h1>
    <div class="hero-sub">{E(s.get('subtitle'))}</div>
    <p class="hero-body">{E(s.get('body'))}</p>
  </div>
</section>"""


def sec_features(s, pool):
    """지그재그 — 큰 번호가 배경에 겹치고 텍스트가 좌우 교차."""
    u = data_uri(pool.take("features"))
    photo = f'<div class="wide-photo"><img src="{u}"></div>' if u else ""
    rows = []
    for i, it in enumerate(s.get("items") or [], 1):
        side = "l" if i % 2 else "r"
        rows.append(f"""<div class="zz {side}">
      <div class="zz-num">{i:02d}</div>
      <div class="zz-txt">
        <div class="zz-name"><i class="ck">\u2713</i>{E(it.get('name'))}</div>
        <div class="zz-desc">{E(it.get('desc'))}</div>
      </div>
    </div>""")
    return f"""<section class="pad cream diag">
  <div class="head"><div class="eyebrow acc">Key Features</div><h2>{E(s.get('title'))}</h2></div>
  {photo}
  <div class="zz-wrap">{''.join(rows)}</div>
</section>"""


def sec_compat(s, pool):
    """칩 배열 — 목록형 정보는 카드보다 칩이 훨씬 밀도가 높다."""
    chips = "".join(
        f'<div class="chip"><b>{E(it.get("name"))}</b><span>{E(it.get("desc"))}</span></div>'
        for it in (s.get("items") or []))
    body = f'<p class="lead">{E(s.get("body"))}</p>' if s.get("body") else ""
    # ★호환 원본표는 칩과 내용이 겹쳐 중복이었다(2026-08-08). 칩만 남긴다 —
    #   한글이라 읽히고, 연식까지 정확히 나오며, 세로도 훨씬 짧다.
    photo = ""
    return f"""<section class="pad ink">
  <div class="head"><div class="eyebrow acc">Compatibility</div><h2 class="on-ink">{E(s.get('title'))}</h2></div>
  {body}
  {photo}
  <div class="chips">{chips}</div>
</section>"""


def sec_howto(s, pool):
    """세로 타임라인 — 단계는 순서가 보여야 한다."""
    u = data_uri(pool.take("howto"))
    photo = f'<div class="wide-photo"><img src="{u}"></div>' if u else ""
    steps = []
    for i, it in enumerate(s.get("items") or [], 1):
        steps.append(f"""<div class="step">
      <div class="step-dot">{i}</div>
      <div class="step-txt"><b>{E(it.get('name'))}</b><span>{E(it.get('desc'))}</span></div>
    </div>""")
    return f"""<section class="pad">
  <div class="head"><div class="eyebrow acc">How to use</div><h2>{E(s.get('title'))}</h2></div>
  {photo}
  <div class="steps">{''.join(steps)}</div>
</section>"""


def sec_spec(s, pool):
    """좌우 대비표 — 값이 커야 스캔된다.

    ★2026-08-15 이미지 자리 추가. howto 가 없는 상품(영양제·도서 등)은 여기가
      3번째 이미지 자리다. SECTION_WANT 가 ("spec","detail") 을 원하므로
      M15 의 features3(topic=detail)가 그대로 걸린다.
    ★풀에 맞는 컷이 없으면 u 가 비어 photo 가 사라진다 — 종전과 같은 모양이 된다.
    """
    u = data_uri(pool.take("spec_highlight"))
    photo = f'<div class="wide-photo"><img src="{u}"></div>' if u else ""
    rows = "".join(
        f'<div class="srow"><span class="sk">{E(it.get("name"))}</span>'
        f'<span class="sv">{E(it.get("desc"))}</span></div>'
        for it in (s.get("items") or []))
    return f"""<section class="pad cream">
  <div class="head"><div class="eyebrow acc">Specifications</div><h2>{E(s.get('title'))}</h2></div>
  {photo}
  <div class="spec">{rows}</div>
</section>"""


def sec_safety(s, pool):
    items = "".join(
        f'<li><b>{E(it.get("name"))}</b> {E(it.get("desc"))}</li>'
        for it in (s.get("items") or []))
    return f"""<section class="pad">
  <div class="warn">
    <div class="warn-mark">!</div>
    <div class="warn-body">
      <h3>{E(s.get('title'))}</h3>
      <ul>{items}</ul>
    </div>
  </div>
</section>"""


def sec_care(s, pool):
    items = "".join(
        f'<div class="crow"><b>{E(it.get("name"))}</b><span>{E(it.get("desc"))}</span></div>'
        for it in (s.get("items") or []))
    return f"""<section class="pad cream">
  <div class="head"><div class="eyebrow acc">Care</div><h2>{E(s.get('title'))}</h2></div>
  <div class="care">{items}</div>
</section>"""


def esc(t):
    """HTML 이스케이프 — 리뷰 원문에 <, & 가 섞여 온다."""
    import html as _h
    return _h.escape(str(t or ""), quote=False)


# ── 아마존 실구매 리뷰 (2026-08-15) ─────────────────────
#   ★출처를 반드시 밝힌다. 참고 템플릿은 'ken*** 고객님' 형식(=우리 쇼핑몰 후기)인데,
#     아마존 리뷰를 그렇게 실으면 우리 구매자 후기로 오인된다 — 표시광고법 위반이자
#     채널 제재 사유다. "아마존 실구매 리뷰"로 명시하면 같은 효과를 안전하게 낸다.
REVIEW_LIMIT = 3          # 사장 지시 — 3건 고정


def _stars(n=5):
    return "".join('<span class="rv-star">★</span>' for _ in range(int(n or 5)))


def fetch_reviews(asin, limit=REVIEW_LIMIT):
    """번역된 5★ 리뷰를 도움됨 순으로. 없으면 빈 리스트 → 섹션 자체를 만들지 않는다.

    ★(asin, star, helpful DESC) 인덱스를 그대로 탄다. 상품이 수만이어도 비용이 일정하다.
    ★자식에 리뷰가 없으면 그룹 부모 것을 쓴다 — 아마존도 변형끼리 리뷰를 공유한다
      (실측: HOTEC 3개 상품이 모두 39,310개로 같았다).
    """
    if not asin:
        return [], None
    import sqlite3
    con = sqlite3.connect(str(BASE / "backend/purchase/purchase.db"), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        keys = [asin]
        row = con.execute("SELECT group_master_asin FROM products WHERE asin=? LIMIT 1",
                          (asin,)).fetchone()
        if row and row["group_master_asin"]:
            keys.append(row["group_master_asin"])
        for k in keys:
            rv = con.execute(
                "SELECT title_ko, body_ko, helpful, verified FROM product_reviews"
                " WHERE asin=? AND star=5 AND body_ko IS NOT NULL AND body_ko<>''"
                " ORDER BY helpful DESC LIMIT ?", (k, limit)).fetchall()
            if rv:
                rate = con.execute("SELECT avg_star, n_ratings FROM product_rating"
                                   " WHERE asin=?", (k,)).fetchone()
                return [dict(x) for x in rv], (dict(rate) if rate else None)
        return [], None
    except Exception:      # noqa: BLE001 — 리뷰가 없다고 상세페이지를 못 만들면 안 된다
        return [], None
    finally:
        con.close()


def sec_reviews(reviews, rate):
    head = ""
    if rate and rate.get("n_ratings"):
        avg = rate.get("avg_star") or 5
        head = ('<div class="rv-sum">' + _stars(round(avg))
                + '<b>%.1f</b><span>아마존 평가 %s개</span></div>'
                % (avg, format(int(rate["n_ratings"]), ",")))
    cards = []
    for r in reviews:
        # ★도움돼요는 화면에 안 낸다(사장 지시 2026-08-15).
        #   단 정렬에는 계속 쓴다 — 아마존 구매자가 유용하다고 표시한 순서가
        #   우리가 임의로 고르는 것보다 낫다(fetch_reviews 의 ORDER BY helpful DESC).
        meta = ["실구매 확인"] if r.get("verified") else []
        cards.append(
            '<div class="rv-card"><div class="rv-top">' + _stars(5)
            + '<span class="rv-src">amazon.com</span></div>'
            + '<div class="rv-title">%s</div>' % esc(r.get("title_ko"))
            + '<div class="rv-body">%s</div>' % esc(r.get("body_ko"))
            + ('<div class="rv-meta">%s</div>' % " · ".join(meta) if meta else "")
            + "</div>")
    return ('<section class="sec rv-sec"><div class="rv-head">'
            '<h2>아마존 실구매 리뷰</h2>'
            '<p>미국 아마존에서 실제로 구매한 분들이 남긴 후기입니다</p></div>'
            + head + "".join(cards)
            + '<div class="rv-note">amazon.com 에 게시된 구매자 리뷰를 번역했습니다. '
              '본 쇼핑몰의 구매 후기가 아닙니다.</div></section>')


RENDERERS = {"hero": sec_hero, "features": sec_features, "compatibility": sec_compat,
             "howto": sec_howto, "spec_highlight": sec_spec, "safety": sec_safety,
             "care": sec_care}

CSS = f"""
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.css');
@import url('https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&display=swap');
*{{box-sizing:border-box;margin:0;padding:0}}
body{{width:1080px;font-family:'Pretendard','Noto Sans KR',sans-serif;background:#fff;color:{INK};
  -webkit-font-smoothing:antialiased}}
section{{width:1080px}}
.pad{{padding:76px 64px}}
.cream{{background:{CREAM}}}
.ink{{background:{INK}}}
.head{{margin-bottom:40px}}
.eyebrow{{font-family:'Instrument Serif',serif;font-style:italic;font-size:26px;letter-spacing:.5px;
  color:{ACCENT};margin-bottom:10px}}
.eyebrow.acc{{color:{ACCENT}}}
h2{{font-size:48px;font-weight:800;letter-spacing:-1.2px;line-height:1.22}}
h2.on-ink{{color:#fff}}
.lead{{font-size:21px;line-height:1.75;color:rgba(255,255,255,.72);margin-bottom:34px;max-width:800px}}

/* hero — ink 색면 위 초대형 타이포, 사진은 아래에서 겹쳐 올라옴 */
.hero{{background:{INK};padding-bottom:0}}
.hero-band{{padding:84px 64px 96px}}
.hero .eyebrow{{font-size:30px}}
.hero h1{{font-size:86px;font-weight:800;color:#fff;letter-spacing:-2.4px;line-height:1.14;margin-bottom:18px}}
.hero-sub{{font-size:36px;font-weight:600;color:{ACCENT};margin-bottom:22px}}
.hero-body{{font-size:25px;line-height:1.8;color:rgba(255,255,255,.66);max-width:820px}}
.hero-photo{{padding:64px 64px 0}}   /* ★사진이 위 — 종전 translateY(48px) 겹침 보정 제거 */
.hero-photo img{{width:100%;display:block;border-radius:20px;box-shadow:0 24px 60px rgba(0,0,0,.32)}}
.hero + section{{padding-top:124px}}

/* hero — 사진 위 스크림 오버레이(한국 상세 문법). 원본 픽셀 무손상, 하단에만 스크림 */
.hero-ov{{position:relative;background:{INK}}}
.hero-ov-img{{width:100%;display:block}}
/* ★밝은 배경 사진에서는 그라데이션만으로 글자가 안 읽힌다(실측: 흰 배경 제품컷에
   영문 아이콘이 한글 뒤로 비침). 하단은 불투명 색면으로 완전히 덮고 위쪽만 페이드. */
.hero-ov-scrim{{position:absolute;left:0;right:0;bottom:0;height:52%;
  background:linear-gradient(to top,{INK} 0%,{INK} 74%,{INK}E6 88%,{INK}00 100%)}}
.hero-ov-txt{{position:absolute;left:0;right:0;bottom:0;padding:0 64px 54px}}
.hero-ov-txt h1{{font-size:80px;font-weight:800;color:#fff;letter-spacing:-2.6px;line-height:1.12;
  margin-bottom:14px}}
.hero-ov-txt .hero-sub{{font-size:36px;font-weight:700;color:{ACCENT};margin-bottom:18px}}
.hero-ov-txt .hero-body{{font-size:25px;line-height:1.75;color:rgba(255,255,255,.82);max-width:840px}}
.hero-ov-txt .eyebrow{{font-size:34px}}

/* 대각 분할 — 색면이 직각으로만 끊기면 리듬이 죽는다 */
.diag{{clip-path:polygon(0 42px,100% 0,100% 100%,0 100%);margin-top:-42px;padding-top:104px}}

.ck{{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;
  border-radius:50%;background:{ACCENT};color:#fff;font-size:17px;font-style:normal;
  margin-right:11px;vertical-align:3px;font-weight:700}}
.zz.r .ck{{margin-right:0;margin-left:11px;order:2}}
.zz.r .zz-name{{display:flex;align-items:center;justify-content:flex-end}}

.wide-photo{{margin-bottom:44px}}
.wide-photo img{{width:100%;display:block;border-radius:18px}}

/* features — 지그재그, 큰 번호가 배경에 겹침 */
.zz-wrap{{display:flex;flex-direction:column;gap:4px}}
.zz{{position:relative;display:flex;align-items:center;gap:26px;padding:30px 0;
  border-top:1px solid {LINE}}}
.zz.r{{flex-direction:row-reverse;text-align:right}}
.zz-num{{font-family:'Instrument Serif',serif;font-size:76px;line-height:1;color:{ACCENT};
  opacity:.26;min-width:104px;text-align:center}}
.zz-txt{{flex:1}}
.zz-name{{font-size:32px;font-weight:800;letter-spacing:-.8px;margin-bottom:7px}}
.zz-desc{{font-size:20px;color:#6b6b6b;line-height:1.6}}

/* compatibility — 칩 */
.chips{{display:flex;flex-wrap:wrap;gap:12px}}
.chip{{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.2);border-radius:999px;
  padding:15px 26px;display:flex;align-items:baseline;gap:11px}}
.chip b{{font-size:25px;font-weight:800;color:#fff;letter-spacing:-.4px}}
.chip span{{font-size:17px;color:{ACCENT};font-weight:600}}

/* howto — 타임라인 */
.steps{{position:relative;padding-left:16px}}
.steps:before{{content:'';position:absolute;left:47px;top:24px;bottom:24px;width:2px;background:{LINE}}}
.step{{position:relative;display:flex;align-items:flex-start;gap:26px;padding:20px 0}}
.step-dot{{width:64px;height:64px;border-radius:50%;background:{ACCENT};color:#fff;flex:none;
  display:flex;align-items:center;justify-content:center;font-size:28px;font-weight:800;
  position:relative;z-index:1;box-shadow:0 0 0 10px #fff}}
.step-txt{{padding-top:9px}}
.step-txt b{{display:block;font-size:30px;font-weight:800;letter-spacing:-.7px;margin-bottom:6px}}
.step-txt span{{font-size:20px;color:#6b6b6b;line-height:1.6}}

/* spec — 좌우 대비 */
.spec{{border-top:2px solid {INK}}}
.srow{{display:flex;justify-content:space-between;align-items:baseline;gap:24px;
  padding:24px 4px;border-bottom:1px solid {LINE}}}
.sk{{font-size:20px;font-weight:700;color:#7a7a7a;letter-spacing:-.3px}}
.sv{{font-size:29px;font-weight:800;letter-spacing:-.7px;text-align:right}}

/* safety — 경고 블록 */
.warn{{display:flex;gap:30px;background:{INK};border-radius:22px;padding:46px 48px;
  border-left:12px solid {ACCENT}}}
.warn-mark{{font-family:'Instrument Serif',serif;font-size:88px;line-height:.82;color:{ACCENT};flex:none}}
.warn-body h3{{font-size:34px;font-weight:800;color:#fff;margin-bottom:20px;letter-spacing:-.9px}}
.warn-body ul{{list-style:none}}
.warn-body li{{font-size:19px;color:rgba(255,255,255,.76);line-height:1.7;padding:9px 0 9px 20px;
  position:relative}}
.warn-body li:before{{content:'';position:absolute;left:0;top:19px;width:7px;height:7px;
  border-radius:50%;background:{ACCENT}}}
.warn-body li b{{color:#fff}}

/* care */
.care{{display:flex;flex-direction:column;gap:2px}}
.crow{{display:flex;gap:22px;align-items:baseline;padding:22px 0;border-bottom:1px solid {LINE}}}
.crow b{{font-size:23px;font-weight:800;min-width:190px}}
.crow span{{font-size:20px;color:#6b6b6b;line-height:1.6}}

/* ── 아마존 실구매 리뷰 (2026-08-15) ── */
.rv-sec{{background:#FFF8E7;padding:56px 40px}}
.rv-head{{text-align:center;margin-bottom:28px}}
.rv-head h2{{font-size:44px;font-weight:800;color:#1a1a1a;margin:0 0 10px}}
.rv-head p{{font-size:20px;color:#6b6b6b;margin:0}}
.rv-sum{{display:flex;align-items:center;justify-content:center;gap:10px;margin-bottom:26px}}
.rv-sum b{{font-size:30px;color:#1a1a1a}}
.rv-sum span{{font-size:19px;color:#6b6b6b}}
/* ★.rv-sum span 이 (0,1,1) 로 더 강해 별까지 회색이 됐다. 별 규칙을 올린다 */
.rv-star,.rv-sum .rv-star{{color:#FFB400;font-size:26px;letter-spacing:-1px}}
.rv-card{{background:#fff;border-radius:22px;padding:28px 30px;margin-bottom:16px;
  box-shadow:0 2px 10px rgba(0,0,0,.05)}}
.rv-top{{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}}
.rv-src{{font-size:16px;color:#9a9a9a}}
.rv-title{{font-size:25px;font-weight:700;color:#1a1a1a;margin-bottom:8px;line-height:1.35}}
.rv-body{{font-size:21px;color:#4a4a4a;line-height:1.6}}
.rv-meta{{margin-top:14px;font-size:17px;color:#9a9a9a}}
.rv-note{{margin-top:18px;text-align:center;font-size:16px;color:#8a8a8a;line-height:1.5}}
"""


def build_html(plan, m, policy, channel: str = "coupang"):
    """섹션 HTML 조립. channel 로 팔레트를 갈아끼운다(A안, 2026-08-08).

    CSS 는 쿠팡 색으로 짜여 있고, 완성된 HTML 에서 hex 만 치환한다. 쿠팡은 항등
    매핑이라 종전과 바이트 단위로 같다 — 큰 CSS 를 토큰화하다 깨뜨릴 위험이 없다.
    """
    pool = ImagePool(m["images"], policy)
    out = []
    for s in plan.get("sections", []):
        fn = RENDERERS.get(s.get("type"))
        if fn:
            out.append(fn(s, pool))

    # ★리뷰는 기획 모델에 맡기지 않는다 — 데이터가 있으면 붙이고 없으면 생략한다.
    #   모델에 맡기면 없는데 만들거나, 있는데 빠뜨린다.
    try:
        _rv, _rate = fetch_reviews(m.get("asin"))
        if _rv:
            out.append(sec_reviews(_rv, _rate))
    except Exception as _e:      # noqa: BLE001
        print("  [경고] 리뷰 섹션 생략: %s" % _e)
    html = (f"<!DOCTYPE html><html lang='ko'><head><meta charset='UTF-8'>"
            f"<style>{CSS}</style></head><body>{''.join(out)}</body></html>")
    try:
        from backend.purchase.services.detail_palette import recolor
        html = recolor(html, channel)
    except Exception as e:      # 팔레트 실패가 렌더를 막지 않게 — 색보다 산출물이 우선
        print(f"  [경고] 팔레트 적용 실패({channel}): {e} — 기본색으로 진행")
    html = _scale_fonts(html)
    return html


async def shoot_sections(html, outdir: Path, pid: int, channel: str = "coupang"):
    """섹션 단위로 잘라 media/products/{pid}/agent_sec{i}.jpg 로 저장하고
    seo_detail.json 매니페스트를 쓴다.

    ★쿠팡 contents 는 이미 seo_detail.json 매니페스트를 우선 사용하도록 돼 있다
      (coupang_lister.build_detail_contents). 그래서 lister 를 고치지 않고 물릴 수 있다.
    ★한 장으로 붙이면 세로 1만px 이 넘어 쿠팡이 거부할 수 있으므로 섹션별로 나눈다.
    """
    from playwright.async_api import async_playwright
    from PIL import Image
    media = BASE / "backend/purchase/media/products" / str(pid)
    media.mkdir(parents=True, exist_ok=True)
    f = outdir / "page.html"
    f.write_text(html, encoding="utf-8")

    async with async_playwright() as pw:
        b = await pw.chromium.launch(args=["--no-sandbox"])
        c = await b.new_context(viewport={"width": 1080, "height": 1400}, device_scale_factor=2)
        pg = await c.new_page()
        await pg.goto(f.as_uri(), wait_until="networkidle")
        await pg.wait_for_timeout(1200)
        boxes = await pg.evaluate("""() => [...document.querySelectorAll('section')].map(e => {
            const r = e.getBoundingClientRect();
            return {top: Math.round(r.top + window.scrollY), h: Math.round(r.height)};
        })""")
        full = outdir / "_full.jpg"
        await pg.screenshot(path=str(full), full_page=True, type="jpeg", quality=86)
        await b.close()

    im = Image.open(full)
    scale = im.width / 1080
    manifest, saved = [], 0
    for i, bx in enumerate(boxes):
        y0 = int(bx["top"] * scale); y1 = int((bx["top"] + bx["h"]) * scale)
        y1 = min(y1, im.height)
        if y1 - y0 < 40:
            continue
        tile = im.crop((0, y0, im.width, y1))
        if tile.width != 1080:
            tile = tile.resize((1080, round(tile.height * 1080 / tile.width)), Image.LANCZOS)
        if tile.height > 5000:                      # 쿠팡 안전선
            tile = tile.resize((1080, 5000), Image.LANCZOS)
        # 쿠팡은 종전 파일명 유지(=coupang_lister 무수정, 기존 등록분 무영향).
        name = f"agent_sec{i}.jpg" if channel == "coupang" else f"agent_{channel}_sec{i}.jpg"
        fp = media / name
        tile.save(fp, "JPEG", quality=85, optimize=True)
        # ★파일명에 내용 해시를 넣어 URL 을 유니크하게 만든다.
        #   쿠팡은 URL 을 캐시 키로 쓰므로 경로가 같으면 내용이 바뀌어도 이전 이미지를
        #   재사용한다(실측 2026-08-12: 상세를 고쳐 재등록해도 예전 그림이 나갔다).
        import hashlib as _hl
        _h = _hl.md5(fp.read_bytes()).hexdigest()[:8]
        _fp2 = fp.with_name(f"{fp.stem}.{_h}{fp.suffix}")
        fp.replace(_fp2)
        manifest.append(f"/api/pa/images/products/{pid}/{_fp2.name}")
        saved += 1
    mf = "seo_detail.json" if channel == "coupang" else f"seo_detail_{channel}.json"
    (media / mf).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return saved, manifest


async def shoot(html, out: Path):
    from playwright.async_api import async_playwright
    f = out.parent / "page.html"
    f.write_text(html, encoding="utf-8")
    async with async_playwright() as pw:
        b = await pw.chromium.launch(args=["--no-sandbox"])
        c = await b.new_context(viewport={"width": 1080, "height": 1400}, device_scale_factor=2)
        pg = await c.new_page()
        await pg.goto(f.as_uri(), wait_until="networkidle")
        await pg.wait_for_timeout(1200)          # 웹폰트 확정 대기
        await pg.screenshot(path=str(out), full_page=True, type="jpeg", quality=84)
        await b.close()


BANNERS_PRE = ["banner_5_customs.jpg", "banner_1_brand.jpg"]
BANNERS_POST = ["banner_2_shipping.jpg", "banner_3_amazon.jpg", "banner_4_purchase_notice.jpg"]


def assemble(pid: int, agent_jpg: Path, out: Path):
    """실제 쿠팡 contents 순서대로 완성본을 조립한다.
       관세 → 브랜드 → [에이전트 섹션] → 스펙표 → 배송 → 구매대행 → 구매 전 확인"""
    from PIL import Image
    W = 1080
    media = BASE / "backend/purchase/media"
    parts = []

    def add(fp):
        if not Path(fp).is_file():
            return
        im = Image.open(fp).convert("RGB")
        if im.width != W:
            im = im.resize((W, round(im.height * W / im.width)), Image.LANCZOS)
        parts.append(im)

    for b in BANNERS_PRE:
        add(media / "banners" / b)
    add(agent_jpg)                                   # ← 제품컷 자리를 에이전트 섹션이 대체
    try:
        from backend.purchase.services.spec_table import render_spec_table
        if render_spec_table(pid):
            add(media / "products" / str(pid) / "spec.jpg")
    except Exception as e:
        print(f"  스펙표 생략: {e}")
    for b in BANNERS_POST:
        add(media / "banners" / b)

    total = sum(p.height for p in parts)
    canvas = Image.new("RGB", (W, total), "white")
    y = 0
    for p_ in parts:
        canvas.paste(p_, (0, y)); y += p_.height
    canvas.save(out, "JPEG", quality=82, optimize=True)
    return total, len(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pid", type=int)
    ap.add_argument("--policy", default="overlay", choices=["overlay", "quote", "none"])
    ap.add_argument("--install", action="store_true",
                    help="섹션을 media 에 저장하고 매니페스트 작성(등록에 반영)")
    ap.add_argument("--plan-from", type=int, default=None, metavar="PID",
                    help="★다른 pid 의 기획·주제태그를 재사용한다(AI 스킵). "
                         "채널마다 pid 가 갈리는 임포트 파이프라인에서 상세를 통일할 때 쓴다.")
    ap.add_argument("--channels", default="coupang",
                    help="쉼표구분. 예: coupang,smartstore — 채널마다 팔레트를 바꿔 따로 굽는다. "
                         "AI 는 채널 수와 무관하게 1회만 돈다.")
    a = ap.parse_args()
    out = OUT_ROOT / str(a.pid); out.mkdir(parents=True, exist_ok=True)

    m = gather(a.pid)
    kinds = {}
    for im in m["images"]:
        kinds[im["kind"]] = kinds.get(im["kind"], 0) + 1
    print(f"상품 {a.pid}  {m['title_ko'][:44]}")
    print(f"  bullet {len(m['bullets'])} / 속성 {len(m['attrs'])} / 이미지 {len(m['images'])}장(중복제거 후) {kinds}")

    # ── 기획 재사용 준비 ────────────────────────────
    _src_plan = None
    if a.plan_from:
        _sp = OUT_ROOT / str(a.plan_from) / "plan.json"
        if not _sp.exists():
            raise SystemExit("★--plan-from %d 의 plan.json 이 없다 — 그 pid 를 먼저 돌릴 것"
                             % a.plan_from)
        _sm = gather(a.plan_from)
        if not _sm or _sm.get("title_ko") != m.get("title_ko"):
            # ★여기서 막지 않으면 남의 상세가 통째로 들어간다
            raise SystemExit("★--plan-from %d 은 다른 상품이다\n     이쪽: %s\n     저쪽: %s"
                             % (a.plan_from, (m.get("title_ko") or "")[:44],
                                ((_sm or {}).get("title_ko") or "")[:44]))
        _src_plan = json.loads(_sp.read_text(encoding="utf-8"))
        # 주제 태그는 이미지가 완전히 같을 때만 물려받는다
        _mine = [Path(x["path"]).name for x in m["images"]]
        _theirs = [Path(x["path"]).name for x in _sm["images"]]
        _sc = (BASE / "backend/purchase/media/products" / str(a.plan_from) / "img_subject.json")
        _dc = (BASE / "backend/purchase/media/products" / str(a.pid) / "img_subject.json")
        if _mine == _theirs and _sc.exists():
            _dc.parent.mkdir(parents=True, exist_ok=True)
            _dc.write_text(_sc.read_text(encoding="utf-8"), encoding="utf-8")
            print("  기획 재사용 pid=%d (주제 태그도 물려받음)" % a.plan_from)
        else:
            print("  기획 재사용 pid=%d (★이미지가 달라 주제 태그는 다시 뽑는다)" % a.plan_from)

    tags = subject_tags(a.pid, m["images"])
    for im, t in zip(m["images"], tags):
        im["subject"] = t
    print(f"  주제 태그: {[(im['idx'], im['subject']) for im in m['images']]}")

    plan = _src_plan if _src_plan is not None else scrub(plan_and_write(m))
    (out / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  요약: {plan.get('summary_ko','')}")
    for s in plan.get("sections", []):
        print(f"    [{s.get('type')}] {s.get('title','')}")

    import asyncio
    import time as _time
    chans = [c.strip() for c in a.channels.split(",") if c.strip()]
    html = build_html(plan, m, a.policy, chans[0])
    if a.install:
        for ch in chans:
            _h = html if ch == chans[0] else build_html(plan, m, a.policy, ch)
            _t0 = _time.time()
            n, man = asyncio.run(shoot_sections(_h, out, a.pid, ch))
            mf = "seo_detail.json" if ch == "coupang" else f"seo_detail_{ch}.json"
            print(f"\n  ★설치[{ch}] 섹션 {n}장 · {mf} ({len(man)}블록) "
                  f"· {_time.time()-_t0:.1f}초")
    asyncio.run(shoot(html, out / "full_page.jpg"))

    pi = sum(x for x, _ in USAGE); po = sum(y for _, y in USAGE)
    cost = pi * 0.10 / 1e6 + po * 0.40 / 1e6   # flash-lite, 사고토큰 포함
    from PIL import Image
    im = Image.open(out / "full_page.jpg")
    print(f"\n  에이전트 섹션 {im.width}x{im.height}")
    h, n = assemble(a.pid, out / "full_page.jpg", out / "assembled.jpg")
    print(f"  완성본 {W_ASM}x{h} ({n}블록) → {out/'assembled.jpg'}")
    print(f"  AI {len(USAGE)}콜  ≈ {cost*1380:.1f}원")


if __name__ == "__main__":
    main()
