# -*- coding: utf-8 -*-
"""번역 단일 출처 — 리스팅 경로와 백필 잡이 공유(드리프트 방지).

- translate_ko: 영문 상품명 → 한국어(flash-lite). 브랜드 영문 유지, 순수 상품명 한 줄.
- clean_ko_title: 모델 출력에서 프롬프트 에코/마크다운/래퍼 제거.
- is_title_garbage: 저장/게시 전 오염 검증 게이트.

flash RPD/월지출한도 소진(2026-07-01) 대응으로 flash-lite 사용. 자세한 배경은 메모리
reference_gemini_flashlite_quota_fallback 참고.
"""
import os
import re
import time

import requests

_MODEL = "gemini-2.5-flash-lite"

# 모델 출력에서 프롬프트 에코/설명 줄을 걸러내는 마커
_TITLE_ECHO_BAD = ("판매용 한국어", "상품명으로", "60자", "제목만", "자연스럽게 번역",
                   "다음 영문", "아래 영문", "브랜드명 유지", "번역한다", "출력하고", "머리말",
                   "음역")


def gemini_keys() -> list:
    ks = []
    for n in ("GEMINI_API_KEY_5", "GEMINI_API_KEY_FALLBACK", "GEMINI_API_KEY",
              "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"):
        v = os.environ.get(n)
        if v and v not in ks:
            ks.append(v)
    return ks


def clean_ko_title(text):
    """모델 응답에서 '실제 상품명 한 줄'만 추출. 마크다운 헤더/불릿/코드펜스/따옴표/xx래퍼/
    프롬프트 에코를 제거 — 실패하면 None(호출측이 영문 원제 유지)."""
    if not text:
        return None
    t = text.replace("```", " ").strip()
    for line in t.splitlines():
        s = line.strip()
        s = re.sub(r"^#+\s*", "", s)                 # 마크다운 헤더 ##
        s = re.sub(r"^[-*•]+\s*", "", s)             # 불릿
        s = re.sub(r"^\d+[.)]\s*", "", s)            # 번호목록
        s = re.sub(r"^\[[^\]]{0,12}\]\s*", "", s)    # 선행 [브랜드명] 등 플레이스홀더 제거
        s = s.strip().strip('"').strip("'").strip("`").strip("*").strip("_").strip()
        while s.startswith("xx"):
            s = s[2:].strip()
        while s.endswith("xx"):
            s = s[:-2].strip()
        if not s:
            continue
        if any(b in s for b in _TITLE_ECHO_BAD):
            continue
        if any("가" <= c <= "힣" for c in s) and len(s) >= 3:
            return s[:80]
    return None


def is_title_garbage(t) -> bool:
    """저장/게시 전 오염 검증 — True면 쓰지 말 것."""
    if not t or not str(t).strip():
        return True
    n = str(t).strip()
    if n.startswith("##") or n.startswith("**") or n.startswith("#"):
        return True
    if any(b in n for b in ("판매용 한국어", "60자", "제목만", "자연스럽게 번역", "머리말")):
        return True
    if n.startswith("xx") and n.endswith("xx"):      # 소문자 래퍼(대소문자 구분)
        return True
    # 한글이 전혀 없으면(순수 영문) 번역 실패로 간주 — 게이트 목적상 garbage 취급
    if not any("가" <= c <= "힣" for c in n):
        return True
    return False


# ★브랜드 방침(2026-07-01): 파이프라인은 title_ko를 그대로 상품명으로 씀(브랜드 prefix 안 붙임).
#   → 브랜드는 title_ko 안에 '영문 원문 1회'로 유지하고 나머지만 한글. 음역 금지(HASLE→해슬 중복 방지).
#   1차: 브랜드 영문 유지. 2차: 영문 에코 방지 위해 '반드시 한국어(브랜드만 영문)' 더 강하게.
_TR_PROMPTS = (
    "아래 영문 상품명을 한국어 상품명으로 번역해라. 브랜드명·제조사명(고유명사)은 영문 원문 그대로 "
    "맨 앞에 1회만 두고 절대 한글로 음역(예: HASLE→해슬)하지 마라. 나머지 단어는 자연스러운 한국어로 "
    "번역해라. 순수 상품명 텍스트 한 줄만 출력하고 머리말·설명·마크다운(#,*)·따옴표·기호·래퍼는 "
    "절대 붙이지 마라. 60자 이내.\n영문: ",
    "다음 영문 상품명을 한국어로 번역하라(영문 그대로 두지 말 것). 브랜드 고유명사만 영문 1회 유지하고 "
    "나머지는 반드시 한국어로 옮겨라. 한국어 상품명 한 줄만 출력, 다른 말·기호 금지.\n영문: ",
)


# ═══════════════════════════════════════════════════════════
# 도서 전용 상품명 조립 (2026-08-01, 사장 지시)
# ═══════════════════════════════════════════════════════════
# 일반 상품은 translate_ko 로 AI 자유번역하지만, 도서는 제목 자체가 고유명사라
# 번역하면 원제가 소실되고 한국 소비자의 영문 원제 검색에도 안 잡힌다.
# → SP-API 값(원제/저자/제본/판차)으로 조립. AI 미사용.
#   예) Guitar For Dummies (Mark Phillips) 영어원서 페이퍼백 4판

BOOK_NAME_MAX = 100          # 쿠팡 sellerProductName 한도

# 저자 필드에 출판사/법인이 들어오는 사례가 많아 걸러낸다.
_CORP_TOKENS = (
    "corporation", "corp", "inc", "inc.", "llc", "ltd", "limited", "gmbh",
    "press", "publishing", "publishers", "publisher", "publications",
    "books", "book", "media", "group", "company", "co", "co.", "editions",
    "studio", "studios", "productions", "associates", "society", "institute",
)

_BINDING_KO = {
    "paperback": "페이퍼백",
    "hardcover": "하드커버",
    "board_book": "보드북",
    "boardbook": "보드북",
    "spiral_bound": "스프링제본",
    "mass_market_paperback": "문고판",
    "library_binding": "양장",
    "leather_bound": "가죽양장",
    "loose_leaf": "낱장",
    "pamphlet": "소책자",
    "cards": "카드",
    "calendar": "달력",
}


def is_book_facts(facts: dict) -> bool:
    """SP-API facts 로 도서 판정. ABIS_BOOK / display_group='Book'."""
    if not isinstance(facts, dict):
        return False
    if str(facts.get("product_type") or "").upper() == "ABIS_BOOK":
        return True
    if str(facts.get("website_display_group_name") or "").strip().lower() == "book":
        return True
    return False


def _looks_like_corp(name: str) -> bool:
    toks = [t.strip(".,").lower() for t in (name or "").split()]
    return any(t in _CORP_TOKENS for t in toks)


def _pick_author(facts: dict) -> str:
    """저자 1명 선정. 출판사/법인·brand 중복은 제외."""
    authors = facts.get("book_authors") or []
    if isinstance(authors, str):
        authors = [authors]
    brand = str(facts.get("brand") or "").strip().lower()
    manu = str(facts.get("manufacturer") or "").strip().lower()
    for a in authors:
        a = str(a or "").strip()
        if not a or len(a) > 40:
            continue
        low = a.lower()
        if _looks_like_corp(a):          # 법인 접미어
            continue
        if low and (low == brand or low == manu):   # 출판사가 저자칸에 들어온 경우
            continue
        return a
    return ""


def build_book_title(facts: dict, max_len: int = BOOK_NAME_MAX) -> str:
    """도서 상품명 조립. 실패 시 빈 문자열(호출측이 기존 경로로 폴백)."""
    title = str(facts.get("title_en") or "").strip()
    if not title:
        return ""
    author = _pick_author(facts)
    binding = _BINDING_KO.get(str(facts.get("book_binding") or "").strip().lower(), "")
    edition = str(facts.get("book_edition") or "").strip()
    ed_txt = f"{edition}판" if edition and edition.isdigit() else ""

    def _assemble(t, with_author=True, with_binding=True, with_ed=True):
        parts = [t]
        if with_author and author:
            parts.append(f"({author})")
        parts.append("영어원서")
        if with_binding and binding:
            parts.append(binding)
        if with_ed and ed_txt:
            parts.append(ed_txt)
        return " ".join(p for p in parts if p)

    # 길이 축소 순서 (2026-08-01 사장 지시): 판차 → ★부제 → 제본 → 저자 → 강제절단.
    # 저자보다 부제를 먼저 버린다 — 저자는 도서 식별에 중요하고, 부제는 장황해지기만 한다.
    short = title.split(":")[0].strip() or title
    for base, kw in (
        (title, {}),                                   # 원형
        (title, {"with_ed": False}),                   # 판차 제거
        (short, {"with_ed": False}),                   # ★부제 절단 (저자·제본 유지)
        (short, {"with_ed": False, "with_binding": False}),          # 제본 제거
        (short, {"with_ed": False, "with_binding": False, "with_author": False}),  # 저자 제거
    ):
        cand = _assemble(base, **kw)
        if len(cand) <= max_len:
            return cand
    return cand[:max_len].rstrip()


def translate_ko(title_en, glossary=None, extra_rules=None):
    """영문 상품명 → 한국어. 실패 시 None(호출측이 영문 유지).
    ★재시도 라운드: 영문 에코(번역 안 함)·일시 503/429면 더 강한 한글강제 프롬프트로 재시도(2026-07-01).

    glossary     [(영문, 한글), ...] — ★M9.3 옵션 번역과 표기를 맞추기 위한 고정 용어.
                 안 넘기면 종전과 완전히 동일하게 동작한다(가산 변경).
                 미공유 시 실측 사고: 옵션 '20큐빅피트' vs 상품명 '20입방피트'(10건 중 4건).
    extra_rules  추가 지시 한 줄. 일반명사가 영문으로 남았을 때 2차 시도에 쓴다.
    """
    if not title_en:
        return None
    # ★로테이션(2026-07-15): _call_ai_sync 사용 → Gemini 소진 시 GPT 자동전환. 라운드별 강한 한글강제 유지.
    from backend_shared.ai.service import _call_ai_sync
    extra = ""
    if glossary:
        extra += ("\n[고정 용어 — 반드시 이 한글 표기를 그대로 써라]\n"
                  + "\n".join("%s = %s" % (e, k) for e, k in glossary))
    if extra_rules:
        extra += "\n" + extra_rules
    for rnd in range(3):
        prompt = _TR_PROMPTS[min(rnd, len(_TR_PROMPTS) - 1)]
        if extra:
            # 프롬프트가 "\n영문: " 로 끝난다 — 그 앞에 끼워 넣는다
            prompt = prompt.replace("\n영문: ", extra + "\n영문: ")
        raw = _call_ai_sync(prompt + title_en, max_tokens=300)
        if raw:
            t = clean_ko_title(raw)
            if t and not is_title_garbage(t):
                return t
            # 응답은 왔으나 한글 없음(영문 에코) → 다음 라운드 더 강한 프롬프트
        time.sleep(0.2)
    return None
