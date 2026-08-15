# -*- coding: utf-8 -*-
"""채널별 상세페이지 팔레트 (2026-08-08).

설계 원칙
---------
1. **조립 후 치환**. PA_SECTION_* 의 큰 HTML 상수는 건드리지 않는다. 완성된 HTML 에서
   hex 만 1:1 로 바꾼다. 기본 팔레트는 항등 매핑이라 **바이트 단위로 종전과 동일**함이
   보장된다 — 대규모 문자열 상수를 토큰화하다 오타로 HTML 을 깨뜨리는 사고를 원천 차단.

2. **브랜드색만 바꾸고 의미색은 지킨다.** 잉크/액센트/크림 계열은 채널 아이덴티티라
   바꾸지만, 빨강(경고)·금색(정품)은 뜻을 담고 있어 채널이 달라도 같아야 한다.

3. **역할(role) 단위로 매핑.** 같은 하늘색이라도 "잉크의 옅은 톤"인지 "성공 표시"인지에
   따라 목적지가 다르다. 현행 hex → 역할 → 채널별 hex 의 2단 구조.

주의 — 액센트 1개가 두 역할을 겸한다
------------------------------------
현행 `#E8845A` 는 (a) 남색 위 텍스트 (b) 흰 텍스트를 얹는 배경 두 곳에 함께 쓰인다.
한 hex 로 두 역할을 만족시킬 수 없어 현행 팔레트는 WCAG 2건이 미달이다
(흰 텍스트 on 액센트 2.67:1, 액센트 on 크림 2.45:1). 신규 팔레트는 두 역할의
중간 명도를 잡아 양쪽 모두 3:1 을 넘겼다. 완전한 해결은 HTML 에서 역할을 분리해
액센트를 2톤으로 쪼개야 하며, 그건 별건이다.
"""
from __future__ import annotations

import re

__all__ = ["recolor", "PALETTE_VERSION", "supported_platforms"]

# 팔레트를 고치면 올린다. ai_processor.PA_TEMPLATE_VERSION 과 함께 재생성 트리거가 된다.
PALETTE_VERSION = "p1"

# ── 현행 색상 → 역할 이름 ──────────────────────────────────────────────
#    ai_processor.PA_SECTION_* 에서 실제로 쓰이는 hex 전수 (2026-08-08 기준)
ROLE_OF = {
    # 잉크(딥네이비) 계열 — 채널 아이덴티티
    "#1B3A5C": "ink",
    "#0F2640": "ink_deep",
    "#15304D": "ink_mid",
    "#254B73": "ink_light",
    "#F5F8FC": "ink_t1",
    "#D6E4F0": "ink_t2",
    "#B8CDE0": "ink_t3",
    "#8FB0CC": "ink_t4",
    # 액센트(코랄) 계열 — 채널 아이덴티티
    "#E8845A": "accent",
    "#F5D9CB": "accent_tint",
    "#FFF4F0": "accent_bg1",
    "#FDF6F2": "accent_bg2",
    # 바탕
    "#F7F5F0": "cream",
    # 에이전트 섹션(detail_agent.py) — 고지배너와 색값이 달라 역할을 따로 둔다.
    #   배너 잉크 #1B3A5C vs 섹션 잉크 #14213D 로 어긋나 있는데, 역할을 분리해 두면
    #   채널별로 각각 맞출 수 있고 쿠팡은 현행 그대로 유지된다.
    "#14213D": "a_ink",
    "#E8582D": "a_accent",
    "#F4F1EA": "a_cream",
    "#DCD5C8": "a_line",
    # 배송 흐름의 '국내 구간' 표시 — 미국 구간(잉크)과 구분되어야 한다
    "#2D8B5E": "flow",
    "#F0FAF5": "flow_bg",
    "#B8E6D0": "flow_line",
}

# ── 의미색 — 채널이 바뀌어도 유지 ────────────────────────────────────
#    빨강=경고/금지, 금색=정품인증. 뜻을 담은 색이라 브랜딩으로 덮지 않는다.
SEMANTIC = {"#D94040", "#F5D77A", "#D4A843", "#5C3D0E", "#F5B841"}

# ── 채널별 팔레트 ────────────────────────────────────────────────────
_BASE = {
    "ink": "#1B3A5C", "ink_deep": "#0F2640", "ink_mid": "#15304D", "ink_light": "#254B73",
    "ink_t1": "#F5F8FC", "ink_t2": "#D6E4F0", "ink_t3": "#B8CDE0", "ink_t4": "#8FB0CC",
    "accent": "#E8845A", "accent_tint": "#F5D9CB",
    "accent_bg1": "#FFF4F0", "accent_bg2": "#FDF6F2",
    "cream": "#F7F5F0",
    "flow": "#2D8B5E", "flow_bg": "#F0FAF5", "flow_line": "#B8E6D0",
    "a_ink": "#14213D", "a_accent": "#E8582D", "a_cream": "#F4F1EA", "a_line": "#DCD5C8",
}

PALETTES: dict[str, dict[str, str]] = {
    # 쿠팡 — 현행 그대로(항등). HTML 경로는 지금 네이버만 쓰지만, 쿠팡이 HTML 로
    # 넘어오거나 회귀 검증을 할 때의 기준선 역할을 한다.
    "coupang": dict(_BASE),

    # 네이버 — 딥그린. 신계정이 화장품·식품 전용이라 자연·클린 인상과도 맞는다.
    # ★brand 가 초록이므로 배송흐름의 '국내 구간' 초록을 그대로 두면 잉크와
    #   구분이 사라진다. 청록으로 옮겨 두 구간의 시각적 분리를 유지한다.
    "smartstore": {
        "ink": "#123A2E", "ink_deep": "#08211A", "ink_mid": "#0D3125", "ink_light": "#1E5140",
        "ink_t1": "#F4F9F6", "ink_t2": "#CFE6DA", "ink_t3": "#A8CDBB", "ink_t4": "#7FB49C",
        "accent": "#2E9E63", "accent_tint": "#C7E6D4",
        "accent_bg1": "#F0FAF4", "accent_bg2": "#F4FAF6",
        "cream": "#F1F4EE",
        "flow": "#2F7D5A", "flow_bg": "#EEF6F1", "flow_line": "#C8E2D5",
        "a_ink": "#0E2E24", "a_accent": "#2E9E63", "a_cream": "#F1F4EE", "a_line": "#D3DCD3",
    },

    # 11번가 — 차콜 + 레드. 11번가 레드 톤을 쓰되 차콜로 눌러 저가 인상을 피한다.
    "elevenst": {
        "ink": "#2A2A2E", "ink_deep": "#141417", "ink_mid": "#232327", "ink_light": "#3D3D44",
        "ink_t1": "#F7F7F8", "ink_t2": "#DEDEE2", "ink_t3": "#BFBFC6", "ink_t4": "#9797A0",
        "accent": "#E05A6E", "accent_tint": "#F6D2D8",
        "accent_bg1": "#FFF4F6", "accent_bg2": "#FDF6F7",
        "cream": "#F5F2F2",
        "flow": "#2D8B5E", "flow_bg": "#F0FAF5", "flow_line": "#B8E6D0",
        "a_ink": "#1C1C20", "a_accent": "#E05A6E", "a_cream": "#F5F2F2", "a_line": "#DDD8D8",
    },

    # ESM(G마켓·옥션) — 딥퍼플. 두 채널 UI 가 초록·빨강이라 겹치지 않게 보라로 뺀다.
    "esm": {
        "ink": "#2E1B3D", "ink_deep": "#180D22", "ink_mid": "#271733", "ink_light": "#452A5B",
        "ink_t1": "#F8F6FA", "ink_t2": "#E0D6E8", "ink_t3": "#C4B2D2", "ink_t4": "#A38CB8",
        "accent": "#9B6BC9", "accent_tint": "#E0D0F0",
        "accent_bg1": "#F8F4FC", "accent_bg2": "#FAF7FC",
        "cream": "#F3F0F4",
        "flow": "#2D8B5E", "flow_bg": "#F0FAF5", "flow_line": "#B8E6D0",
        "a_ink": "#241432", "a_accent": "#9B6BC9", "a_cream": "#F3F0F4", "a_line": "#DCD5E0",
    },
}

# 채널 별칭 — 호출부가 쓰는 platform 문자열이 제각각이라 흡수한다.
_ALIAS = {
    "naver": "smartstore", "smart_store": "smartstore", "smartstore": "smartstore",
    "coupang": "coupang", "cp": "coupang",
    "11st": "elevenst", "elevenst": "elevenst", "eleven": "elevenst",
    "esm": "esm", "gmarket": "esm", "auction": "esm",
}

_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}")


def supported_platforms() -> list[str]:
    return sorted(PALETTES)


def recolor(html: str, platform: str = "smartstore") -> str:
    """완성된 상세 HTML 의 브랜드색을 채널 팔레트로 치환.

    - 한 번의 스캔으로 동시 치환한다(순차 치환 시 A→B→C 로 연쇄되는 사고 방지).
    - 역할표에 없는 hex(의미색·회색 등)는 손대지 않는다.
    - 알 수 없는 platform 이면 원본을 그대로 돌려준다 — 색을 못 맞추는 것보다
      깨진 페이지를 내보내는 쪽이 훨씬 나쁘다.
    """
    if not html:
        return html
    key = _ALIAS.get((platform or "").strip().lower())
    pal = PALETTES.get(key or "")
    if not pal:
        return html

    def sub(m: re.Match) -> str:
        hx = m.group(0)
        up = hx.upper()
        if up in SEMANTIC:
            return hx
        role = ROLE_OF.get(up)
        if not role:
            return hx
        return pal.get(role, hx)

    return _HEX_RE.sub(sub, html)
