# -*- coding: utf-8 -*-
"""제품 이미지 저작권 게이트 — 판매자 마케팅 크리에이티브를 갤러리·상세에서 제외.

각 이미지를 다음으로 분류:
  - 'photo'    : 스튜디오(흰/단색 배경) 깨끗한 제품 단독컷 / 제품 패키지(박스) 촬영컷
                 (제품 자체를 보여줌 — 식별 목적, 저위험) → 사용
  - 'lifestyle': 현실 사용환경(야외·실내) 연출컷 또는 사람/모델이 제품을 사용·소지하는 장면.
                 사진이지만 아마존 원본 라이프스타일 — 저작권 + 초상권 위험 → 사용 금지(2026-06-30)
  - 'marketing': 판매자가 만든 마케팅 그래픽/인포그래픽 (사진 위 홍보문구·기능 콜아웃·비교표·
                 슬로건·아이콘 다이어그램·사용법 텍스트 오버레이) — 판매자 저작권/광고문구 IP
                 침해 위험(계정정지 유발 유형) → 사용 금지
  ※ photo 만 갤러리·상세에 사용. 소비처는 `== 'photo'`(미분류는 photo 기본값) 로 게이트.

Gemini 비전 1콜(배치)로 전 이미지 분류 + media/products/{pid}/image_class.json 캐시.
실패 시 차단하지 않고 'photo'로 폴백(리스팅 우선) + 경고 로그.
"""
import os
import json
import time
import base64
import logging
from pathlib import Path

import requests

from backend.purchase.database import get_db

logger = logging.getLogger(__name__)

_VIS_MODEL = "gemini-2.5-flash-lite"  # flash RPD/지출한도 소진(2026-07-01) → 별도 쿼터버킷 flash-lite. 실프롬프트 정확도 5/5 검증
_MEDIA = Path(__file__).resolve().parent.parent / "media"
_MAX_IMAGES = 20  # 한 요청에 보낼 최대 이미지 수

_PROMPT = (
    "아래 {n}개의 상품 이미지를 입력 순서대로 분류한다. 각 이미지를 셋 중 하나로:\n"
    "- \"photo\": 스튜디오 촬영의 깨끗한 제품컷. 흰배경/단색배경에서 제품을 단독으로(또는 손으로 제품을 "
    "들고 있어도 배경이 흰색·단색이면) 촬영한 사진, 제품 패키지(박스) 촬영컷. 핵심 판단=배경이 스튜디오"
    "(흰/단색)이고 제품 식별이 목적. 제품 포장에 원래 인쇄된 고유 문구는 제품의 일부이므로 photo. 단, 흰/단색 배경이어도 소품 스타일링·예술적 구도·드라마틱 조명이나 반사로 창작적으로 '연출'된 제품컷(광고 이미지사진, 대법원 98다43366상 저작권 보호대상)은 photo 아님(lifestyle).\n"
    "- \"lifestyle\": 사진이지만 실제 사용환경/생활 장면 배경에서 연출된 라이프스타일컷. 야외(잔디·캠핑장·"
    "해변)·실내(거실·주방·텐트 안) 등 현실 배경이거나, 사람(얼굴·전신 모델)이 제품을 사용·소지하는 장면. "
    "핵심 판단=배경이 스튜디오가 아닌 현실 사용환경이거나 모델이 등장.\n"
    "- \"marketing\": 판매자가 제작한 마케팅 그래픽/인포그래픽. 사진 위에 덧입힌 홍보 문구·기능 설명 라벨"
    "·콜아웃·비교표·아이콘 다이어그램·슬로건·사용법 단계 텍스트 등이 합성된 이미지.\n"
    "우선순위: 텍스트/그래픽 오버레이가 있으면(배경 무관) marketing. 오버레이 없는 순수 사진이면 "
    "배경이 스튜디오(흰/단색)이고 '기계적 제품 촬영'이면 photo, 현실 사용환경·모델 등장이거나 소품 스타일링·예술적 연출의 창작 제품컷이면 lifestyle.\n"
    "JSON 배열로만 출력: [\"photo\"|\"lifestyle\"|\"marketing\", ...] (정확히 {n}개, 입력 순서대로). 설명 금지."
)


def _gemini_keys() -> list[str]:
    keys: list[str] = []
    for name in ("GEMINI_API_KEY", "GEMINI_API_KEY_FALLBACK"):
        v = os.environ.get(name, "")
        if v and v not in keys:
            keys.append(v)
    for n in range(2, 10):
        v = os.environ.get(f"GEMINI_API_KEY_{n}", "")
        if v and v not in keys:
            keys.append(v)
    return keys


def _cache_path(pid) -> Path:
    return _MEDIA / "products" / str(pid) / "image_class.json"


def image_paths(pid) -> list[str]:
    """제품 이미지 local_path 목록 (image_idx 순, 존재하는 파일만)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT local_path FROM image_cache WHERE product_id=? AND public_url IS NOT NULL "
            "ORDER BY image_idx ASC",
            (pid,),
        ).fetchall()
    return [r["local_path"] for r in rows if r["local_path"] and os.path.isfile(r["local_path"])]


class ClassifyFailed(Exception):
    """비전 분류가 재시도 후에도 실패 — 조용한 all-photo 누출 방지를 위해 호출측에 신호."""
    pass


def _vision_classify(paths: list[str]) -> dict[str, str]:
    if os.environ.get("PA_SKIP_GEMINI") == "1":
        raise ClassifyFailed("PA_SKIP_GEMINI=1 (Gemini 스킵)")  # -> all-photo 폴백(빠름)
    keys = _gemini_keys()
    if not keys:
        # 키 미설정 = 설정 오류. all-photo 누출 대신 실패로 신호(호출측이 보류 판단).
        raise ClassifyFailed("GEMINI_API_KEY 없음")
    sub = paths[:_MAX_IMAGES]
    parts = [{"text": _PROMPT.format(n=len(sub))}]
    for p in sub:
        try:
            b = base64.b64encode(Path(p).read_bytes()).decode()
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b}})
        except Exception as e:
            logger.warning(f"[img-class] 이미지 로드 실패 {p}: {e}")
            parts.append({"text": "(이미지 로드 실패 — photo 로 간주)"})
    last = None
    # ★신뢰성: 일시 실패(429/5xx/타임아웃) 대비 2라운드 키 로테이션 + 백오프. 전부 실패하면
    #   조용한 all-photo 폴백 대신 ClassifyFailed 를 던져 마케팅 누출 차단(호출측이 등록 보류).
    for rnd in range(2):
        all_429 = True   # 이 라운드가 전부 429(쿼터/지출한도)면 재시도 무의미 → 조기 포기
        for key in keys:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{_VIS_MODEL}:generateContent?key={key}"
            try:
                r = requests.post(url, timeout=90, json={
                    "contents": [{"parts": parts}],
                    "generationConfig": {"temperature": 0, "thinkingConfig": {"thinkingBudget": 0}},
                })
                if r.status_code >= 400:
                    last = f"status={r.status_code} {r.text[:120]}"
                    if r.status_code != 429:
                        all_429 = False   # 5xx/기타는 일시오류 → 재시도 가치
                    continue
                t = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                arr, _ = json.JSONDecoder().raw_decode(t[t.find("["):].strip())
                out: dict[str, str] = {}
                for i, p in enumerate(paths):
                    lab = arr[i] if i < len(arr) else "photo"
                    s = str(lab).strip().lower()
                    if s.startswith("m"):
                        out[p] = "marketing"
                    elif s.startswith("l"):
                        out[p] = "lifestyle"     # 인물/실사용환경 연출컷 — 저작권·초상권 위험 → 제외
                    else:
                        out[p] = "photo"
                return out
            except Exception as e:
                last = e
                all_429 = False   # 예외(타임아웃 등)는 일시오류 → 재시도 가치
        # ★전 키 429(쿼터/월 지출한도 초과)면 2라운드 재시도해도 소용없음 → 즉시 포기(요청 낭비 방지, 2026-07-01)
        if all_429:
            break
        time.sleep(1.5 * (rnd + 1))
    raise ClassifyFailed(str(last))


def _read_cache(cp, paths):
    if cp.exists():
        try:
            cached = json.loads(cp.read_text())
            if all(p in cached for p in paths):
                return {p: cached[p] for p in paths}
        except Exception:
            pass
    return None


def classify_images(pid, force: bool = False) -> dict[str, str]:
    """제품 이미지 분류 dict {local_path: 'photo'|'marketing'}. 캐시 우선.
    ★비전 실패 시 all-photo 를 캐시하지 않음(다음 회차 재시도) + 과거 성공 캐시가 있으면 그걸 우선."""
    paths = image_paths(pid)
    if not paths:
        return {}
    cp = _cache_path(pid)
    if not force:
        c = _read_cache(cp, paths)
        if c is not None:
            return c
    try:
        result = _vision_classify(paths)
    except ClassifyFailed as e:
        logger.warning(f"[img-class] product {pid} 분류 실패 — 미캐시 폴백: {e}")
        c = _read_cache(cp, paths)   # 과거 성공 캐시가 all-photo 보다 안전
        return c if c is not None else {p: "photo" for p in paths}
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"[img-class] 캐시 저장 실패 {pid}: {e}")
    n_mk = sum(1 for v in result.values() if v == "marketing")
    n_lf = sum(1 for v in result.values() if v == "lifestyle")
    logger.info(f"[img-class] product {pid}: {len(paths)}장 중 marketing {n_mk}장 + lifestyle {n_lf}장 제외")
    return result


def classify_reliable(pid, force: bool = True) -> tuple:
    """(분류dict, ok). 비전 분류가 실제 성공했는지 ok 로 알림 — 마이그레이션 누출 방지용
    (ok=False 면 등록 보류 후 다음 회차 재시도). 이미지 없음은 게이트 무관이라 ok=True."""
    paths = image_paths(pid)
    if not paths:
        return {}, True
    # ★볼륨 절감(2026-07-01): force=False 면 유효 캐시 우선 — Gemini 재호출 없이 캐시 반환.
    #   기존엔 force 무시하고 매번 _vision_classify 호출 → 재시작·defer마다 재분류로 RPD/지출 폭증.
    #   신규(캐시無)만 실제 분류. 캐시 히트는 ok=True(이미 성공했던 결과).
    if not force:
        c = _read_cache(_cache_path(pid), paths)
        if c is not None:
            return c, True
    try:
        result = _vision_classify(paths)
    except ClassifyFailed as e:
        logger.warning(f"[img-class] product {pid} reliable 실패: {e}")
        return {p: "photo" for p in paths}, False
    try:
        cp = _cache_path(pid)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(result, ensure_ascii=False))
    except Exception:
        pass
    return result, True


def clean_local_paths(pid, force: bool = False) -> list[str]:
    """마케팅 그래픽 제외한 '깨끗한 제품 사진' local_path 목록 (image_idx 순).

    전부 marketing 으로 분류돼 빈 결과면, 대표 식별을 위해 첫 이미지 1장은 유지(폴백).
    """
    paths = image_paths(pid)
    if not paths:
        return []
    cls = classify_images(pid, force=force)
    clean = [p for p in paths if cls.get(p, "photo") == "photo"]
    if not clean:
        logger.warning(f"[img-class] product {pid}: 클린 이미지 0장 — 대표 1장만 유지(폴백)")
        return paths[:1]
    return clean
