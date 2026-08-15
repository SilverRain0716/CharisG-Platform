"""지식재산권(IP) 사전검수 — 한국특허정보원 KIPRIS Plus 오픈API 연동 게이트.

등록 전 단계에서 상품 브랜드/상품명을 국내 등록 지식재산권 권리자와 대조하여
침해 위험 상품을 자동 플래그 → 수동검토(manual review)로 회부한다.

서비스: CommonSearchService/CommonSearchApplicantInfo (지식재산권 통합검색 — 권리자 조회)
응답:   <commonSearchPersonInfo><Name>..</Name><EnglishName/><Address/><PersonNumber/>...

쿼터:   월 1,000건 무료 → 동일 키워드 in-memory 캐시로 중복 호출 절감.
키:     KIPRIS_ACCESS_KEY (https://plus.kipris.or.kr 무료 발급). 미설정 시 fail-safe '대기'.
"""
from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET

import requests

KIPRIS_KEY = os.environ.get("KIPRIS_ACCESS_KEY", "").strip()
BASE = "http://plus.kipris.or.kr/openapi/rest"
TIMEOUT = 12
RETRIES = 2          # 일시적 연결 실패 재시도
RETRY_DELAY = 0.6    # 초 — 레이트 완화

# 동일 키워드 재조회 방지 (쿼터 절감) — 프로세스 수명 동안 유효
_CACHE: dict[str, list[dict]] = {}


def is_enabled() -> bool:
    return bool(KIPRIS_KEY)


class KiprisUnavailable(Exception):
    """조회 불가(쿼터 초과 resultCode 22, 파라미터 오류 등) — '미발견'과 구분."""


def search_rights_holder(name: str, start: int = 1) -> list[dict]:
    """권리자(출원인/등록권리자) 명의로 국내 등록 지식재산권 조회."""
    key = name.strip()
    if key in _CACHE:
        return _CACHE[key]
    url = f"{BASE}/CommonSearchService/CommonSearchApplicantInfo"
    params = {"searchName": key, "docsStart": start, "accessKey": KIPRIS_KEY}
    last_exc = None
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            code = (root.findtext(".//resultCode") or "").strip()
            if code and code not in ("00",):
                # 22=무료횟수 초과, 10=파라미터 오류 등 → '미발견' 아님(조회 불가)
                raise KiprisUnavailable(f"{code}:{(root.findtext('.//resultMsg') or '').strip()}")
            out = []
            for item in root.iter("commonSearchPersonInfo"):
                rec = {c.tag: (c.text or "").strip() for c in item}
                out.append({
                    "name": rec.get("Name", ""),
                    "english_name": rec.get("EnglishName", ""),
                    "address": rec.get("Address", ""),
                    "person_number": rec.get("PersonNumber", ""),
                })
            _CACHE[key] = out
            return out
        except KiprisUnavailable:
            raise  # 재시도 무의미(쿼터/파라미터)
        except Exception as e:
            last_exc = e
            if attempt < RETRIES:
                time.sleep(RETRY_DELAY)
    raise last_exc


def screen(brand: str, title_ko: str = "") -> dict:
    """브랜드/상품명 → 국내 등록 IP 권리자 대조.

    Returns:
        {enabled, flagged, status, reason, query, matches[]}
    """
    if not is_enabled():
        return {"enabled": False, "flagged": None, "status": "대기",
                "reason": "KIPRIS Plus accessKey 미등록 — 발급 시 자동 활성",
                "query": "", "matches": []}

    # 브랜드명만 권리자 대조 (상품 일반어 오탐 방지). 브랜드 없으면 검수 보류.
    kw = (brand or "").strip()
    if len(kw) < 2:
        return {"enabled": True, "flagged": False, "status": "통과",
                "reason": "브랜드 미상 — 권리자 대조 생략",
                "query": "", "matches": []}
    try:
        hits = search_rights_holder(kw)
    except KiprisUnavailable as e:  # 쿼터/오류 = 조회 불가 → '통과' 아님, '대기'
        return {"enabled": True, "flagged": None, "status": "대기",
                "reason": f"KIPRIS 조회 불가(쿼터/오류 {e}) — 캐시 결과 유지, 쿼터 리셋 후 재조회",
                "query": kw, "matches": []}
    except Exception as e:  # 일시 네트워크 오류 → 대기
        return {"enabled": True, "flagged": None, "status": "대기",
                "reason": f"KIPRIS 일시 조회 실패: {e}",
                "query": kw, "matches": []}
    if hits:
        return {"enabled": True, "flagged": True, "status": "수동검토",
                "reason": f"'{kw}' 국내 등록 지식재산권 권리자 {len(hits)}건 확인 → 수동검토 회부",
                "query": kw, "matches": hits[:5]}
    return {"enabled": True, "flagged": False, "status": "통과",
            "reason": "국내 등록 IP 권리자 미발견",
            "query": kw, "matches": []}
